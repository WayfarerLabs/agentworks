"""Typed source resolution, bounded lifetime, and value containment."""

from __future__ import annotations

import inspect
import sys
from collections.abc import ItemsView, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import FrozenInstanceError
from typing import Any, ClassVar, Literal

import pytest
from pydantic import BaseModel

from agentworks import output
from agentworks.capabilities.secret_backend import SecretBackend
from agentworks.capabilities.secret_backend.client import (
    InteractionBroker,
    RemainingTime,
    SecretClientFailure,
    SecretClientFailureKind,
    SecretClientRemediation,
    SecretClientTimeout,
    SecretLookupRequest,
    SecretSourceClient,
)
from agentworks.errors import ExternalError, SecretUnavailableError
from agentworks.plugins import Plugin, seated_plugin
from agentworks.resources.graph import Readiness
from agentworks.schema import AgwModel, AgwRootModel, CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import (
    OUTCOME_RULES,
    ResolutionCategory,
    ResolutionDetail,
    ResolutionOutcome,
    ResolutionRemediation,
    format_remediation,
)
from agentworks.secrets.policy import InteractionPolicy
from agentworks.secrets.resolve import (
    _BATCH_TOKEN,
    ActiveSource,
    CompletionPolicy,
    OutputInteractionBroker,
    ResolutionBatch,
    ResolutionPolicy,
    _drive_source,
    resolve_batch,
)


class _Config(AgwModel):
    name: Literal["fixture"]


class _Mapping(AgwRootModel[str]):
    pass


class _NullableConfig(AgwModel):
    name: Literal["nullable"]


class _NullableMapping(AgwRootModel[str | None]):
    pass


class _Client:
    def __init__(
        self,
        events: list[str],
        values: dict[str, str],
        failure: BaseException | None,
    ) -> None:
        self.events = events
        self.values = values
        self.failure = failure

    def prepare(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> None:
        self.events.append("prepare")

    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> dict[str, str]:
        self.events.append("resolve")
        if self.failure is not None:
            raise self.failure
        return {request.name: self.values[request.name] for request in requests if request.name in self.values}


class _Context(AbstractContextManager[SecretSourceClient]):
    def __init__(self, client: _Client, events: list[str]) -> None:
        self.client = client
        self.events = events

    def __enter__(self) -> SecretSourceClient:
        self.events.append("enter")
        return self.client

    def __exit__(self, *args: object) -> None:
        self.events.append("exit")


class _Backend(SecretBackend):
    contract_version: ClassVar[int] = 2
    config_model: ClassVar[type[AgwModel]] = _Config
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = _Mapping
    name: ClassVar[str] = "fixture"
    description: ClassVar[str] = "fixture"
    prose = None
    interactive: ClassVar[bool] = False
    events: ClassVar[list[str]] = []
    values: ClassVar[dict[str, str]] = {}
    failure: ClassVar[BaseException | None] = None

    @classmethod
    def backend_readiness(cls) -> Readiness:
        return Readiness.ready()

    @classmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
        return True

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> str | None:
        return f"id:{secret_name}"

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        cls.events.append("factory")
        return _Context(_Client(cls.events, cls.values, cls.failure), cls.events)


class _NullableBackend(_Backend):
    config_model: ClassVar[type[AgwModel]] = _NullableConfig
    mapping_model: ClassVar[type[AgwRootModel[Any]]] = _NullableMapping
    name: ClassVar[str] = "nullable"

    @classmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
        return mapping_present

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> str | None:
        assert isinstance(mapping, _NullableMapping)
        assert mapping.root is None
        return f"id:{secret_name}"


class _BrokerCapturingBackend(_Backend):
    brokers: ClassVar[list[InteractionBroker | None]] = []

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        cls.brokers.append(interaction_broker)
        return super().create_client(
            source_name=source_name,
            config=config,
            interaction_broker=interaction_broker,
            remaining_time=remaining_time,
        )


def _source(
    *,
    ready: bool = True,
    name: str = "primary",
    backend_class: type[_Backend] = _Backend,
) -> ActiveSource:
    return ActiveSource(
        source=SecretSourceDecl(name=name, backend=CapabilityBlock.of("fixture")),
        backend_class=backend_class,
        config=_Config(name="fixture"),
        readiness=Readiness.ready() if ready else Readiness.blocked("fixture unavailable"),
    )


def _policy(
    *,
    interaction: InteractionPolicy = InteractionPolicy.REFUSE,
    completion: CompletionPolicy = CompletionPolicy.COMPLETE,
) -> ResolutionPolicy:
    return ResolutionPolicy(interaction=interaction, completion=completion)


def _recursive_agentworks_traceback_text(exc: BaseException) -> str:
    values: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        traceback = current.__traceback__
        while traceback is not None:
            if traceback.tb_frame.f_globals.get("__name__", "").startswith("agentworks."):
                values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
            traceback = traceback.tb_next
        current = current.__cause__ or current.__context__
    return "\n".join(values)


@contextmanager
def _interrupt_exact_line(function: Any, source_line: str) -> Iterator[None]:
    lines, first_line = inspect.getsourcelines(function)
    matches = [first_line + index for index, line in enumerate(lines) if line.rstrip("\n") == source_line]
    assert matches, (function, source_line)
    target_line = matches[0]
    target_code = function.__code__
    fired = False

    def trace(frame: Any, event: str, argument: object) -> Any:
        del argument
        nonlocal fired
        if not fired and frame.f_code is target_code and event == "line" and frame.f_lineno == target_line:
            fired = True
            sys.settrace(None)
            raise KeyboardInterrupt
        return trace

    sys.settrace(trace)
    try:
        yield
    finally:
        sys.settrace(None)


@pytest.fixture(autouse=True)
def _reset_backend() -> None:
    _Backend.events = []
    _Backend.values = {}
    _Backend.failure = None
    _BrokerCapturingBackend.brokers = []


def test_one_lazy_client_turn_and_redacted_batch() -> None:
    _Backend.values = {"token": "sentinel-value"}

    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source()],
        policy=_policy(),
        interaction_broker=None,
    )

    assert _Backend.events == ["factory", "enter", "prepare", "resolve", "exit"]
    assert batch.outcomes[0].category is ResolutionCategory.RESOLVED
    assert batch.outcomes[0].source == "primary"
    assert "sentinel-value" not in repr(batch)
    assert batch.complete_or_raise() == {"token": "sentinel-value"}


def test_allow_scopes_live_output_broker_to_prompt_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.capabilities.secret_backend.prompt import PromptBackend, PromptSourceConfig

    secret = SecretDecl(name="token", description="token")
    prompt_source = ActiveSource(
        source=SecretSourceDecl(name="prompt", backend=CapabilityBlock.of("prompt")),
        backend_class=PromptBackend,
        config=PromptSourceConfig(name="prompt"),
        readiness=Readiness.ready(),
    )
    broker = OutputInteractionBroker([secret])
    monkeypatch.setattr(output, "prompt_secret", lambda label, *, hint: "prompt-value")

    batch = resolve_batch(
        [secret],
        [_source(backend_class=_BrokerCapturingBackend), prompt_source],
        policy=_policy(interaction=InteractionPolicy.ALLOW),
        interaction_broker=broker,
    )

    assert _BrokerCapturingBackend.brokers == [None]
    assert batch.complete_or_raise() == {"token": "prompt-value"}


class _PostReturnClient(_Client):
    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> dict[str, str]:
        self.events.append("resolve")
        return {request.name: "sentinel-post-return-value" for request in requests}

    def __repr__(self) -> str:
        return "_PostReturnClient(sentinel-provider-client)"


class _PostReturnContext(AbstractContextManager[SecretSourceClient]):
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.client = _PostReturnClient(events, {}, None)

    def __enter__(self) -> SecretSourceClient:
        self.events.append("enter")
        return self.client

    def __exit__(self, *args: object) -> None:
        self.events.append("exit")

    def __repr__(self) -> str:
        return "_PostReturnContext(sentinel-provider-context)"


class _PostReturnBackend(_Backend):
    events: ClassVar[list[str]] = []

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        cls.events.append("factory")
        return _PostReturnContext(cls.events)


def test_post_resolve_boundary_interrupt_clears_mapping_client_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = 0

    def interrupt_after_resolve(remaining_time: RemainingTime) -> None:
        del remaining_time
        nonlocal checks
        checks += 1
        if checks == 5:
            raise KeyboardInterrupt

    _PostReturnBackend.events = []
    monkeypatch.setattr("agentworks.secrets.resolve._check_boundary", interrupt_after_resolve)
    with pytest.raises(KeyboardInterrupt) as caught:
        resolve_batch(
            [SecretDecl(name="token", description="token")],
            [_source(backend_class=_PostReturnBackend)],
            policy=_policy(),
            interaction_broker=None,
        )

    retained = _recursive_agentworks_traceback_text(caught.value)
    assert "sentinel-post-return-value" not in retained
    assert "sentinel-provider-client" not in retained
    assert "sentinel-provider-context" not in retained
    assert _PostReturnBackend.events == ["factory", "enter", "prepare", "resolve", "exit"]


@pytest.mark.parametrize(
    "source_line",
    ["        client = None", "        return resolved"],
    ids=["first-post-context-line", "successful-return"],
)
def test_exact_drive_source_boundaries_clear_mapping_client_and_context(source_line: str) -> None:
    _PostReturnBackend.events = []
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        _interrupt_exact_line(_drive_source, source_line),
    ):
        resolve_batch(
            [SecretDecl(name="token", description="token")],
            [_source(backend_class=_PostReturnBackend)],
            policy=_policy(),
            interaction_broker=None,
        )

    retained = _recursive_agentworks_traceback_text(caught.value)
    assert "sentinel-post-return-value" not in retained
    assert "sentinel-provider-client" not in retained
    assert "sentinel-provider-context" not in retained


def test_explicit_json_null_mapping_reaches_nullable_mapping_model() -> None:
    _NullableBackend.events = []
    _NullableBackend.values = {"token": "resolved-from-null"}
    source = ActiveSource(
        source=SecretSourceDecl(name="nullable-source", backend=CapabilityBlock.of("nullable")),
        backend_class=_NullableBackend,
        config=_NullableConfig(name="nullable"),
        readiness=Readiness.ready(),
    )
    decl = SecretDecl(
        name="token",
        description="token",
        backend_mappings={"nullable-source": None},
    )
    plugin = Plugin(name="nullable-mapping", capabilities={"secret-backend": (_NullableBackend,)})
    with seated_plugin(plugin):
        batch = resolve_batch([decl], [source], policy=_policy(), interaction_broker=None)

    assert batch.complete_or_raise() == {"token": "resolved-from-null"}
    assert _NullableBackend.events == ["factory", "enter", "prepare", "resolve", "exit"]


def test_soft_miss_is_typed_and_incomplete_batch_clears_values() -> None:
    batch = resolve_batch(
        [SecretDecl(name="missing", description="missing")],
        [_source()],
        policy=_policy(),
        interaction_broker=None,
    )

    outcome = batch.outcomes[0]
    assert (outcome.category, outcome.detail, outcome.remediation) == (
        ResolutionCategory.UNAVAILABLE,
        ResolutionDetail.SOFT_MISS,
        ResolutionRemediation.CONFIGURE_SOURCE,
    )
    with pytest.raises(SecretUnavailableError):
        batch.complete_or_raise()
    assert batch._values == {}


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        (
            SecretClientFailure(
                kind=SecretClientFailureKind.HARD_MAPPING,
                remediation=SecretClientRemediation.CHECK_MAPPING,
            ),
            ResolutionDetail.HARD_MAPPING,
        ),
        (SecretClientTimeout(), ResolutionDetail.DEADLINE_EXCEEDED),
        (RuntimeError("sentinel-native-error"), ResolutionDetail.UNEXPECTED),
    ],
)
def test_source_failures_are_value_free_and_batch_attributed(
    failure: BaseException,
    detail: ResolutionDetail,
) -> None:
    _Backend.failure = failure
    batch = resolve_batch(
        [SecretDecl(name="a", description="a"), SecretDecl(name="b", description="b")],
        [_source()],
        policy=_policy(),
        interaction_broker=None,
    )

    assert [outcome.detail for outcome in batch.outcomes] == [detail, detail]
    assert "sentinel-native-error" not in repr(batch.outcomes)
    assert _Backend.events[-1] == "exit"


def test_not_ready_source_constructs_nothing() -> None:
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source(ready=False)],
        policy=_policy(),
        interaction_broker=None,
    )

    assert _Backend.events == []
    assert batch.outcomes[0].detail is ResolutionDetail.SOURCE_NOT_READY


def test_control_characters_hard_fail_before_resolved() -> None:
    _Backend.values = {"token": "line-one\nline-two"}
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source()],
        policy=_policy(),
        interaction_broker=None,
    )

    assert batch.outcomes[0].detail is ResolutionDetail.MALFORMED_VALUE
    assert "line-one" not in repr(batch)


def test_duplicate_name_uses_first_declaration_once() -> None:
    _Backend.values = {"token": "value"}
    first = SecretDecl(name="token", description="first")
    second = SecretDecl(name="token", description="second")

    batch = resolve_batch([first, second], [_source()], policy=_policy(), interaction_broker=None)

    assert [outcome.name for outcome in batch.outcomes] == ["token"]
    assert _Backend.events.count("resolve") == 1


def test_empty_input_constructs_no_client() -> None:
    batch = resolve_batch([], [_source()], policy=_policy(), interaction_broker=None)
    assert batch.outcomes == ()
    assert batch.complete_or_raise() == {}
    assert _Backend.events == []


def test_request_and_outcome_records_are_frozen_slotted_and_value_free() -> None:
    request = SecretLookupRequest(name="token", mapping=_Mapping("address"))
    with pytest.raises(FrozenInstanceError):
        request.name = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        vars(request)
    assert not hasattr(request, "description")
    assert not hasattr(request, "value")

    _Backend.values = {"token": "sentinel-value"}
    outcome = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source()],
        policy=_policy(),
        interaction_broker=None,
    ).outcomes[0]
    with pytest.raises(FrozenInstanceError):
        outcome.name = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        vars(outcome)
    assert "sentinel-value" not in repr(outcome)
    assert not hasattr(outcome, "value")


def test_exact_five_categories_and_exhaustive_detail_table() -> None:
    assert {category.value for category in ResolutionCategory} == {
        "resolved",
        "unavailable",
        "refused-interaction",
        "timeout",
        "resolution-failure",
    }
    assert set(OUTCOME_RULES) == set(ResolutionDetail)
    for detail, rule in OUTCOME_RULES.items():
        outcome = ResolutionOutcome(
            name="token",
            category=rule.category,
            detail=detail,
            remediation=rule.remediation,
            source="source" if rule.source_required else None,
            identifier="identifier" if rule.identifier_allowed else None,
            remediation_target="Vault.Plugin" if rule.remediation_target_required else None,
        )
        assert outcome.detail is detail


def test_illegal_outcome_tuple_is_rejected() -> None:
    with pytest.raises(ValueError, match="category or remediation"):
        ResolutionOutcome(
            name="token",
            category=ResolutionCategory.RESOLVED,
            detail=ResolutionDetail.SOFT_MISS,
            remediation=ResolutionRemediation.NONE,
            source="source",
        )
    with pytest.raises(ValueError, match="source"):
        ResolutionOutcome(
            name="token",
            category=ResolutionCategory.RESOLVED,
            detail=ResolutionDetail.RESOLVED,
            remediation=ResolutionRemediation.NONE,
        )
    with pytest.raises(ValueError, match="category or remediation"):
        ResolutionOutcome(
            name="token",
            category=ResolutionCategory.RESOLVED,
            detail=ResolutionDetail.RESOLVED,
            remediation=ResolutionRemediation.ENABLE_PLUGIN,
            source="source",
            remediation_target="onepassword",
        )
    with pytest.raises(ValueError, match="target presence"):
        ResolutionOutcome(
            name="token",
            category=ResolutionCategory.UNAVAILABLE,
            detail=ResolutionDetail.SOURCE_NOT_READY,
            remediation=ResolutionRemediation.ENABLE_SOURCE,
            source="source",
            remediation_target="Vault.Plugin",
        )
    with pytest.raises(ValueError, match="target presence"):
        ResolutionOutcome(
            name="token",
            category=ResolutionCategory.UNAVAILABLE,
            detail=ResolutionDetail.SOURCE_BACKEND_PLUGIN_DISABLED,
            remediation=ResolutionRemediation.ENABLE_PLUGIN,
            source="source",
        )


@pytest.mark.parametrize("target", ["", "plugin/name"])
def test_enable_plugin_outcome_rejects_only_registration_invalid_targets(target: str) -> None:
    with pytest.raises(ValueError, match="non-empty and '/'-free"):
        ResolutionOutcome(
            name="token",
            category=ResolutionCategory.UNAVAILABLE,
            detail=ResolutionDetail.SOURCE_BACKEND_PLUGIN_DISABLED,
            remediation=ResolutionRemediation.ENABLE_PLUGIN,
            source="source",
            remediation_target=target,
        )


@pytest.mark.parametrize(
    ("target", "rendered"),
    [
        ("onepassword", "enable plugin `onepassword`"),
        ("Vault.Plugin", "enable plugin `Vault.Plugin`"),
        ("weird`\\\n雪", r"enable plugin `weird\x60\x5c\x0a\u96ea`"),
    ],
)
def test_enable_plugin_remediation_uses_fixed_ascii_safe_rendering(target: str, rendered: str) -> None:
    outcome = ResolutionOutcome(
        name="token",
        category=ResolutionCategory.UNAVAILABLE,
        detail=ResolutionDetail.SOURCE_BACKEND_PLUGIN_DISABLED,
        remediation=ResolutionRemediation.ENABLE_PLUGIN,
        source="source",
        remediation_target=target,
    )
    assert format_remediation(outcome) == rendered


def test_enable_plugin_target_accepts_a_registered_string_subclass() -> None:
    class PluginName(str):
        pass

    outcome = ResolutionOutcome(
        name="token",
        category=ResolutionCategory.UNAVAILABLE,
        detail=ResolutionDetail.SOURCE_BACKEND_PLUGIN_DISABLED,
        remediation=ResolutionRemediation.ENABLE_PLUGIN,
        source="source",
        remediation_target=PluginName("Vault.Plugin"),
    )
    assert format_remediation(outcome) == "enable plugin `Vault.Plugin`"


def test_batch_has_no_generic_value_surface_and_success_returns_a_copy() -> None:
    _Backend.values = {"token": "sentinel-value"}
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source()],
        policy=_policy(),
        interaction_broker=None,
    )
    for attribute in ("model_dump", "dict", "asdict", "values", "partial_values"):
        assert not hasattr(batch, attribute)
    with pytest.raises(TypeError):
        vars(batch)
    with pytest.raises(TypeError):
        iter(batch)  # type: ignore[call-overload]
    assert str(batch) == repr(batch)
    first = batch.complete_or_raise()
    first["token"] = "changed"
    assert batch.complete_or_raise() == {"token": "sentinel-value"}


@pytest.mark.parametrize(
    "source_line",
    [
        "            self._values: dict[str, str] = {}",
        "                if type(name) is not str or type(value) is not str:",
        "            return None",
    ],
    ids=["slot-initialization", "after-value-extraction", "successful-return"],
)
def test_exact_batch_constructor_interrupt_clears_every_plaintext_copy(source_line: str) -> None:
    outcome = ResolutionOutcome(
        name="token",
        category=ResolutionCategory.RESOLVED,
        detail=ResolutionDetail.RESOLVED,
        remediation=ResolutionRemediation.NONE,
        source="primary",
    )
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        _interrupt_exact_line(ResolutionBatch.__init__, source_line),
    ):
        ResolutionBatch(
            (outcome,),
            {"token": "sentinel-constructor-copy"},
            _token=_BATCH_TOKEN,
        )

    assert "sentinel-constructor-copy" not in _recursive_agentworks_traceback_text(caught.value)
    traceback_names: list[str] = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        traceback_names.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "<genexpr>" not in traceback_names
    partial = object.__new__(ResolutionBatch)
    assert repr(partial) == "ResolutionBatch(outcomes=0, resolved=0, values=<redacted>)"


def test_incomplete_batch_clears_resolved_values_before_error_traceback() -> None:
    _Backend.values = {"resolved": "sentinel-value"}
    batch = resolve_batch(
        [SecretDecl(name="resolved", description="resolved"), SecretDecl(name="missing", description="missing")],
        [_source()],
        policy=_policy(),
        interaction_broker=None,
    )
    with pytest.raises(SecretUnavailableError) as caught:
        batch.complete_or_raise()
    assert batch._values == {}
    assert "sentinel-value" not in repr(caught.value)
    frames = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    retained = [frame.f_locals.get("self") for frame in frames if frame.f_code.co_name == "complete_or_raise"]
    assert retained == [batch]
    assert retained[0]._values == {}


def test_one_source_receives_one_ordered_batch() -> None:
    _Backend.values = {"b": "2", "a": "1"}
    batch = resolve_batch(
        [SecretDecl(name="a", description="a"), SecretDecl(name="b", description="b")],
        [_source()],
        policy=_policy(),
        interaction_broker=None,
    )
    assert _Backend.events == ["factory", "enter", "prepare", "resolve", "exit"]
    assert [outcome.name for outcome in batch.outcomes] == ["a", "b"]
    assert batch.complete_or_raise() == {"a": "1", "b": "2"}


class _ProtocolClient(_Client):
    returned: ClassVar[object] = {}

    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> Any:
        self.events.append("resolve")
        return self.returned


class _ProtocolBackend(_Backend):
    events: ClassVar[list[str]] = []
    values: ClassVar[dict[str, str]] = {}
    failure: ClassVar[BaseException | None] = None

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        cls.events.append("factory")
        return _Context(_ProtocolClient(cls.events, {}, None), cls.events)


@pytest.mark.parametrize("returned", [{"extra": "value"}, {"token": object()}, object(), None])
def test_provider_protocol_violation_fails_the_whole_attempted_batch(returned: object) -> None:
    _ProtocolBackend.events = []
    _ProtocolClient.returned = returned
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source(backend_class=_ProtocolBackend)],
        policy=_policy(completion=CompletionPolicy.PARTIAL),
        interaction_broker=None,
    )
    assert batch.outcomes[0].detail is ResolutionDetail.BACKEND_PROTOCOL


class _HostileMapping(Mapping[str, str]):
    def __init__(self, phase: str) -> None:
        self.phase = phase

    def __iter__(self) -> Iterator[str]:
        if self.phase == "iteration":
            raise RuntimeError("sentinel-hostile-iteration")
        return iter(("token",))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        if self.phase == "indexing":
            raise RuntimeError("sentinel-hostile-indexing")
        return "sentinel-provider-value"

    def __contains__(self, key: object) -> bool:
        if self.phase == "membership":
            raise RuntimeError("sentinel-hostile-membership")
        return super().__contains__(key)

    def items(self) -> ItemsView[str, str]:
        if self.phase == "items":
            raise RuntimeError("sentinel-hostile-items")
        return super().items()

    def __repr__(self) -> str:
        return f"_HostileMapping(sentinel-hostile-{self.phase})"


@pytest.mark.parametrize("phase", ["items", "iteration", "indexing"])
def test_hostile_provider_mapping_traversal_is_sanitized_and_value_free(phase: str) -> None:
    _ProtocolBackend.events = []
    _ProtocolClient.returned = _HostileMapping(phase)
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source(backend_class=_ProtocolBackend)],
        policy=_policy(completion=CompletionPolicy.PARTIAL),
        interaction_broker=None,
    )

    assert batch.outcomes[0].detail is ResolutionDetail.UNEXPECTED
    with pytest.raises(ExternalError) as caught:
        batch.complete_or_raise()
    traceback_text: list[str] = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("agentworks."):
            traceback_text.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    assert "sentinel-hostile" not in "\n".join(traceback_text)
    assert "sentinel-provider-value" not in "\n".join(traceback_text)


def test_provider_membership_is_never_consulted_after_safe_snapshot() -> None:
    _ProtocolBackend.events = []
    _ProtocolClient.returned = _HostileMapping("membership")
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source(backend_class=_ProtocolBackend)],
        policy=_policy(),
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"token": "sentinel-provider-value"}


def test_exact_inlined_snapshot_entry_clears_provider_without_callee_transfer() -> None:
    _ProtocolBackend.events = []
    _ProtocolClient.returned = _HostileMapping("membership")
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        _interrupt_exact_line(resolve_batch, "            returned_values = {}"),
    ):
        resolve_batch(
            [SecretDecl(name="token", description="token")],
            [_source(backend_class=_ProtocolBackend)],
            policy=_policy(),
            interaction_broker=None,
        )

    retained = _recursive_agentworks_traceback_text(caught.value)
    assert "sentinel-hostile" not in retained
    assert "sentinel-provider-value" not in retained


def test_exact_batch_finalization_interrupt_clears_resolved_values() -> None:
    _Backend.values = {"token": "sentinel-finalization-value"}
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        _interrupt_exact_line(resolve_batch, "        for secret in deduped:"),
    ):
        resolve_batch(
            [SecretDecl(name="token", description="token")],
            [_source()],
            policy=_policy(),
            interaction_broker=None,
        )

    assert "sentinel-finalization-value" not in _recursive_agentworks_traceback_text(caught.value)


def test_exact_batch_return_interrupt_clears_constructed_batch_values() -> None:
    _Backend.values = {"token": "sentinel-batch-return-value"}
    with (
        pytest.raises(KeyboardInterrupt) as caught,
        _interrupt_exact_line(resolve_batch, "        return batch"),
    ):
        resolve_batch(
            [SecretDecl(name="token", description="token")],
            [_source()],
            policy=_policy(),
            interaction_broker=None,
        )

    assert "sentinel-batch-return-value" not in _recursive_agentworks_traceback_text(caught.value)


@pytest.mark.parametrize("value", ["\0x", "x\0", "\rx", "x\r", "\nx", "x\n", "x\ny"])
def test_every_transport_control_character_position_is_rejected(value: str) -> None:
    _Backend.values = {"token": value}
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source()],
        policy=_policy(completion=CompletionPolicy.PARTIAL),
        interaction_broker=None,
    )
    assert batch.outcomes[0].detail is ResolutionDetail.MALFORMED_VALUE
    assert value not in repr(batch)


@pytest.mark.parametrize("value", ["", "plain", "\ttab", " leading", "trailing "])
def test_other_string_values_retain_existing_transport_behavior(value: str) -> None:
    _Backend.values = {"token": value}
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source()],
        policy=_policy(),
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"token": value}
