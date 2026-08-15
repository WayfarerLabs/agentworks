"""Typed source resolution, bounded lifetime, and value containment."""

from __future__ import annotations

from contextlib import AbstractContextManager
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
from agentworks.errors import ExternalError, SecretUnavailableError, StateError
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
    ActiveSource,
    CompletionPolicy,
    OutputInteractionBroker,
    ResolutionPolicy,
    resolve_batch,
    resolve_partial_for_reveal,
)

_VALUE_SENTINEL = "secret-value-sentinel"


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


@pytest.fixture(autouse=True)
def _reset_backend() -> None:
    _Backend.events = []
    _Backend.values = {}
    _Backend.failure = None
    _BrokerCapturingBackend.brokers = []


def test_policy_construction_rejects_a_non_enum_interaction() -> None:
    """The totality the eight boundary check sites cannot buy: a seventh construction that
    took ``interaction`` from a parameter and never checked it would still raise here.
    Every consumer compares the field by identity, so a plain ``"refuse"`` is the value
    that resolves through an interactive source in a run that meant to refuse.
    """
    with pytest.raises(StateError):
        ResolutionPolicy(interaction="refuse", completion=CompletionPolicy.COMPLETE)  # type: ignore[arg-type]


def test_partial_reveal_rejects_a_non_enum_policy_before_any_source_work() -> None:
    """``resolve_partial_for_reveal`` constructs a ``ResolutionPolicy``, so it is a
    consumer and checks its own argument rather than trusting ``show_env``, its only
    caller today, to have checked. It is module-level and importable, and "it has one
    caller" is what left the fault this check exists to close reachable."""
    with pytest.raises(StateError):
        resolve_partial_for_reveal(
            [SecretDecl(name="token", description="")],
            [_source()],
            interaction="refuse",  # type: ignore[arg-type]
        )
    assert _Backend.events == []


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


def test_soft_miss_is_typed_and_incomplete_batch_raises() -> None:
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


def test_nul_hard_fails_before_resolved() -> None:
    _Backend.values = {"token": "line-one\0line-two"}
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


def test_incomplete_batch_error_does_not_expose_resolved_values() -> None:
    _Backend.values = {"resolved": "sentinel-value"}
    batch = resolve_batch(
        [SecretDecl(name="resolved", description="resolved"), SecretDecl(name="missing", description="missing")],
        [_source()],
        policy=_policy(),
        interaction_broker=None,
    )
    with pytest.raises(SecretUnavailableError) as caught:
        batch.complete_or_raise()
    assert "sentinel-value" not in repr(caught.value)


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


@pytest.mark.parametrize("value", [f"{_VALUE_SENTINEL}\0x", f"x\0{_VALUE_SENTINEL}"])
def test_every_nul_position_is_rejected(value: str) -> None:
    _Backend.values = {"token": value}
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source()],
        policy=_policy(completion=CompletionPolicy.PARTIAL),
        interaction_broker=None,
    )
    assert batch.outcomes[0].detail is ResolutionDetail.MALFORMED_VALUE
    assert _VALUE_SENTINEL not in repr(batch)

    with pytest.raises(ExternalError) as caught:
        batch.complete_or_raise()

    rendered_graph = repr((caught.value.args, vars(caught.value), caught.value.__cause__, caught.value.__context__))
    assert _VALUE_SENTINEL not in rendered_graph
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain",
        "\ttab",
        " leading",
        "trailing ",
        "line-one\nline-two\n",
        "line-one\r\nline-two\r\n",
    ],
)
def test_all_non_nul_string_values_are_preserved(value: str) -> None:
    _Backend.values = {"token": value}
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source()],
        policy=_policy(),
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"token": value}
