"""Typed source resolution, bounded lifetime, and value containment."""

from __future__ import annotations

import inspect
import sys
from collections.abc import ItemsView, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import FrozenInstanceError
from types import TracebackType
from typing import Any, ClassVar, Literal, cast

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
from agentworks.errors import SecretUnavailableError, StateError
from agentworks.plugins import Plugin, seated_plugin
from agentworks.resources.graph import Readiness
from agentworks.schema import AgwModel, AgwRootModel, CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.resolve import (
    _BATCH_TOKEN,
    _OUTCOME_RULES,
    ActiveSource,
    CompletionPolicy,
    InteractionPolicy,
    ResolutionBatch,
    ResolutionCategory,
    ResolutionDetail,
    ResolutionOutcome,
    ResolutionPolicy,
    ResolutionRemediation,
    _drive_source,
    _inspection_projection,
    _SourceContextDriver,
    resolve_batch,
    resolve_secrets,
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


def test_output_handler_interrupt_clears_legacy_wrapper_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _Backend.values = {"token": "sentinel-wrapper-value"}
    monkeypatch.setattr(output, "is_interactive", lambda: False)
    monkeypatch.setattr(output, "info", lambda message: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt) as caught:
        resolve_secrets(
            [SecretDecl(name="token", description="token")],
            [_source()],
        )

    assert "sentinel-wrapper-value" not in _recursive_agentworks_traceback_text(caught.value)


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
    assert set(_OUTCOME_RULES) == set(ResolutionDetail)
    for detail, (category, remediation, source_required, identifier_allowed) in _OUTCOME_RULES.items():
        outcome = __import__("agentworks.secrets.resolve", fromlist=["ResolutionOutcome"]).ResolutionOutcome(
            name="token",
            category=category,
            detail=detail,
            remediation=remediation,
            source="source" if source_required else None,
            identifier="identifier" if identifier_allowed else None,
        )
        assert outcome.detail is detail


def test_illegal_outcome_tuple_is_rejected() -> None:
    from agentworks.secrets.resolve import ResolutionOutcome

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
    with pytest.raises(SecretUnavailableError) as caught:
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


class _InterruptingErrors(dict[str, str]):
    def __setitem__(self, key: str, value: str) -> None:
        del key, value
        raise KeyboardInterrupt


def test_partial_legacy_error_projection_interrupt_clears_plaintext_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Backend.values = {"resolved": "sentinel-partial-errors-value"}
    monkeypatch.setattr(output, "is_interactive", lambda: False)

    with pytest.raises(KeyboardInterrupt) as caught:
        resolve_secrets(
            [
                SecretDecl(name="resolved", description="resolved"),
                SecretDecl(name="missing", description="missing"),
            ],
            [_source()],
            errors=_InterruptingErrors(),
        )

    assert "sentinel-partial-errors-value" not in _recursive_agentworks_traceback_text(caught.value)


def test_actual_partial_legacy_projection_return_interrupt_clears_plaintext_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Backend.values = {"token": "sentinel-inspection-return-value"}
    monkeypatch.setattr(output, "is_interactive", lambda: False)

    with (
        pytest.raises(KeyboardInterrupt) as caught,
        _interrupt_exact_line(_inspection_projection, "        return values, outcomes"),
    ):
        resolve_secrets(
            [SecretDecl(name="token", description="token")],
            [_source()],
            errors={},
        )

    assert "sentinel-inspection-return-value" not in _recursive_agentworks_traceback_text(caught.value)


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


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _TimedClient(_Client):
    phase: ClassVar[str] = ""
    clock: ClassVar[_Clock]

    def prepare(self, requests: tuple[SecretLookupRequest, ...], *, remaining_time: RemainingTime) -> None:
        self.events.append("prepare")
        if self.phase == "prepare":
            self.clock.now = 2.0

    def resolve(
        self,
        requests: tuple[SecretLookupRequest, ...],
        *,
        remaining_time: RemainingTime,
    ) -> dict[str, str]:
        self.events.append("resolve")
        if self.phase == "resolve":
            self.clock.now = 2.0
        return {request.name: "discarded" for request in requests}


class _TimedContext(AbstractContextManager[SecretSourceClient]):
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> SecretSourceClient:
        self.events.append("enter")
        if _TimedClient.phase == "enter":
            _TimedClient.clock.now = 2.0
        return _TimedClient(self.events, {}, None)

    def __exit__(self, *args: object) -> None:
        self.events.append("exit")


class _TimedBackend(_Backend):
    events: ClassVar[list[str]] = []
    values: ClassVar[dict[str, str]] = {}
    failure: ClassVar[BaseException | None] = None

    @classmethod
    def external_operation_timeout(cls, config: AgwModel) -> float:
        return 1.0

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
        if _TimedClient.phase == "factory":
            _TimedClient.clock.now = 2.0
        return _TimedContext(cls.events)


class _InvalidTimeoutBackend(_Backend):
    timeout_value: ClassVar[object] = None

    @classmethod
    def external_operation_timeout(cls, config: AgwModel) -> float | None:
        return cast("float | None", cls.timeout_value)


@pytest.mark.parametrize("timeout", [True, False, "1", object(), float("nan"), float("inf"), 0, -1])
def test_invalid_external_operation_timeout_is_framework_state_error_before_factory(timeout: object) -> None:
    _InvalidTimeoutBackend.events = []
    _InvalidTimeoutBackend.timeout_value = timeout
    with pytest.raises(StateError, match="invalid external-operation timeout"):
        resolve_batch(
            [SecretDecl(name="token", description="token")],
            [_source(backend_class=_InvalidTimeoutBackend)],
            policy=_policy(completion=CompletionPolicy.PARTIAL),
            interaction_broker=None,
        )
    assert _InvalidTimeoutBackend.events == []


@pytest.mark.parametrize(
    ("phase", "events"),
    [
        ("factory", ["factory"]),
        ("enter", ["factory", "enter", "exit"]),
        ("prepare", ["factory", "enter", "prepare", "exit"]),
        ("resolve", ["factory", "enter", "prepare", "resolve", "exit"]),
    ],
)
def test_timeout_at_each_external_boundary_stops_later_work(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    events: list[str],
) -> None:
    clock = _Clock()
    _TimedBackend.events = []
    _TimedClient.phase = phase
    _TimedClient.clock = clock
    monkeypatch.setattr("agentworks.secrets.resolve.time.monotonic", clock)
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source(backend_class=_TimedBackend)],
        policy=_policy(completion=CompletionPolicy.PARTIAL),
        interaction_broker=None,
    )
    assert batch.outcomes[0].detail is ResolutionDetail.DEADLINE_EXCEEDED
    assert _TimedBackend.events == events


class _ExitContext(AbstractContextManager[object]):
    def __init__(self, *, result: object = False, failure: BaseException | None = None) -> None:
        self.result = result
        self.failure = failure
        self.exc_info: tuple[object, object, object] | None = None

    def __enter__(self) -> object:
        return object()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        exc_info = (exc_type, exc, traceback)
        self.exc_info = exc_info
        if self.failure is not None:
            raise self.failure
        return bool(self.result)


def test_cleanup_receives_exact_exc_info_and_never_suppresses(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(output, "warn", warnings.append)
    inner = _ExitContext(result=True)
    driver = _SourceContextDriver(inner, source_name="fixture-source", remaining_time=lambda: None)
    with pytest.raises(KeyboardInterrupt) as caught, driver:
        raise KeyboardInterrupt
    assert inner.exc_info is not None
    assert inner.exc_info[0] is KeyboardInterrupt
    assert inner.exc_info[1] is caught.value
    assert inner.exc_info[2] is caught.value.__traceback__
    assert warnings == ["secret source 'fixture-source': cleanup failed; primary result unchanged"]


@pytest.mark.parametrize(
    ("start", "finish", "result", "failure", "warns"),
    [
        (1.0, 0.0, False, None, True),
        (0.0, 0.0, False, None, False),
        (1.0, 1.0, True, None, True),
        (0.0, 0.0, False, RuntimeError("sentinel-cleanup"), True),
    ],
)
def test_cleanup_warning_matrix_is_non_masking(
    monkeypatch: pytest.MonkeyPatch,
    start: float,
    finish: float,
    result: object,
    failure: BaseException | None,
    warns: bool,
) -> None:
    samples = iter((start, finish))
    warnings: list[str] = []
    monkeypatch.setattr(output, "warn", warnings.append)
    inner = _ExitContext(result=result, failure=failure)
    driver = _SourceContextDriver(inner, source_name="fixture-source", remaining_time=lambda: next(samples))
    with driver:
        pass
    assert bool(warnings) is warns
    assert "sentinel-cleanup" not in repr(warnings)


def test_cleanup_warning_sink_failure_cannot_mask() -> None:
    inner = _ExitContext(result=True)
    driver = _SourceContextDriver(inner, source_name="fixture-source", remaining_time=lambda: None)
    original = output.warn
    output.warn = cast("Any", lambda message: (_ for _ in ()).throw(RuntimeError("sink")))
    try:
        with driver:
            pass
    finally:
        output.warn = original
