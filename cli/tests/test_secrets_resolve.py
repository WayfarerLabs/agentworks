"""Core source-first resolution and provider-aware preview behavior."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import ClassVar, Literal, cast

from pydantic import BaseModel

from agentworks.capabilities.secret_backend import (
    BackendBlocked,
    BackendFailed,
    BackendMissing,
    BackendPreview,
    BackendResolution,
    BackendResolved,
    BlockReason,
    FailureReason,
    IndeterminateReason,
    InteractionBroker,
    LookupDescription,
    LookupDisposition,
    OperatorImpact,
    PreviewAvailable,
    PreviewIndeterminate,
    RemainingTime,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.resources.graph import Readiness
from agentworks.schema import AgwModel, AgwRootModel, CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import ResolutionFailed, ResolutionStatus
from agentworks.secrets.preview import PreviewStatus, preview_batch
from agentworks.secrets.resolve import ActiveSource, resolve_batch


class _Config(AgwModel):
    name: Literal["fixture"]


class _Mapping(AgwRootModel[str]):
    pass


class _Client:
    preview_results: ClassVar[dict[str, BackendPreview]] = {}
    resolution_results: ClassVar[dict[str, BackendResolution]] = {}
    calls: ClassVar[list[tuple[str, ...]]] = []

    def preview(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendPreview]:
        self.calls.append(tuple(request.name for request in requests))
        return {request.name: self.preview_results[request.name] for request in requests}

    def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
        self.calls.append(tuple(request.name for request in requests))
        return {request.name: self.resolution_results[request.name] for request in requests}


class _Backend(SecretBackend):
    name = "fixture"
    description = "fixture"
    contract_version = 1
    config_model: ClassVar[type[AgwModel]] = _Config
    mapping_model = _Mapping
    supports_tty_interaction = False
    client_type: ClassVar[type[_Client]] = _Client

    @classmethod
    def backend_readiness(cls) -> Readiness:
        return Readiness.ready()

    @classmethod
    def describe_lookup(cls, secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        return LookupDescription(LookupDisposition.CANDIDATE, f"id:{secret_name}")

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        return nullcontext(cls.client_type())


def _source(
    name: str,
    *,
    client_type: type[_Client],
    ready: bool = True,
    supports_tty: bool = False,
) -> ActiveSource:
    backend = cast(
        "type[_Backend]",
        type(
            f"{name.title()}Backend",
            (_Backend,),
            {"name": name, "client_type": client_type, "supports_tty_interaction": supports_tty},
        ),
    )
    config_model = cast(
        "type[AgwModel]",
        type(
            f"{name.title()}Config",
            (AgwModel,),
            {"__annotations__": {"name": Literal[name]}},  # type: ignore[valid-type]
        ),
    )
    backend.config_model = config_model
    readiness = Readiness.ready() if ready else Readiness.blocked("not ready")
    return ActiveSource(
        source=SecretSourceDecl(name=name, backend=CapabilityBlock.of(name)),
        backend_class=backend,
        config=config_model.model_validate({"name": name}),
        readiness=readiness,
    )


def _decl(name: str) -> SecretDecl:
    return SecretDecl(name=name, description=name)


def test_actual_resolution_is_one_source_first_pass_with_missing_fallthrough() -> None:
    class First(_Client):
        resolution_results = {"a": BackendMissing(), "b": BackendResolved("first-b")}
        calls = []

    class Second(_Client):
        resolution_results = {"a": BackendResolved("second-a")}
        calls = []

    batch = resolve_batch(
        [_decl("a"), _decl("b")],
        [_source("first", client_type=First), _source("second", client_type=Second)],
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"a": "second-a", "b": "first-b"}
    assert First.calls == [("a", "b")]
    assert Second.calls == [("a",)]


def test_backend_failure_hard_stops_that_secret_but_not_its_siblings() -> None:
    class First(_Client):
        resolution_results = {
            "a": BackendFailed(FailureReason.LOOKUP_REJECTED),
            "b": BackendMissing(),
        }
        calls = []

    class Second(_Client):
        resolution_results = {"b": BackendResolved("second-b")}
        calls = []

    batch = resolve_batch(
        [_decl("a"), _decl("b")],
        [_source("first", client_type=First), _source("second", client_type=Second)],
        tty_access=TtyInteractionAccess.UNAVAILABLE,
        interaction_broker=None,
    )
    assert isinstance(batch.outcomes[0].result, ResolutionFailed)
    assert batch.outcomes[0].result.reason is FailureReason.LOOKUP_REJECTED
    assert batch.outcomes[1].status is ResolutionStatus.RESOLVED
    assert Second.calls == [("b",)]


def test_blocked_source_falls_through_and_is_retained_on_exhaustion() -> None:
    class Blocked(_Client):
        resolution_results = {"a": BackendBlocked(BlockReason.TTY_UNAVAILABLE)}
        calls = []

    class Missing(_Client):
        resolution_results = {"a": BackendMissing()}
        calls = []

    outcome = resolve_batch(
        [_decl("a")],
        [
            _source("promptish", client_type=Blocked, supports_tty=True),
            _source("missing", client_type=Missing),
        ],
        tty_access=TtyInteractionAccess.UNAVAILABLE,
        interaction_broker=None,
    ).outcomes[0]
    assert outcome.status is ResolutionStatus.BLOCKED
    assert outcome.reason is BlockReason.TTY_UNAVAILABLE


def test_preview_uses_later_definitive_result_and_retains_attempt_evidence() -> None:
    class First(_Client):
        preview_results = {"a": PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED)}
        calls = []

    class Second(_Client):
        preview_results = {"a": PreviewAvailable()}
        calls = []

    preview = preview_batch(
        [_decl("a")],
        [_source("first", client_type=First), _source("second", client_type=Second)],
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
        interaction_broker=None,
    )["a"]
    assert preview.status is PreviewStatus.AVAILABLE
    assert [attempt.source for attempt in preview.attempts] == ["first", "second"]


def test_allow_preview_rejects_indeterminate_as_backend_protocol_failure() -> None:
    class Client(_Client):
        preview_results = {"a": PreviewIndeterminate(IndeterminateReason.OPERATOR_IMPACT_LIMITED)}

    preview = preview_batch(
        [_decl("a")],
        [_source("fixture", client_type=Client)],
        impact=OperatorImpact.ALLOW,
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=None,
    )["a"]
    assert preview.status is PreviewStatus.FAILED
    assert preview.reason == FailureReason.BACKEND_PROTOCOL.value


def test_exact_result_map_is_validated_before_any_value_is_copied() -> None:
    class Incomplete(_Client):
        def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
            return {"a": BackendResolved("must-not-escape")}

    batch = resolve_batch(
        [_decl("a"), _decl("b")],
        [_source("fixture", client_type=Incomplete)],
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    )
    assert [outcome.status for outcome in batch.outcomes] == [ResolutionStatus.FAILED, ResolutionStatus.FAILED]
    assert "must-not-escape" not in repr(batch)


def test_false_mapping_is_excluded_before_backend_call() -> None:
    class Client(_Client):
        resolution_results = {"a": BackendResolved("unused")}
        calls = []

    decl = SecretDecl(name="a", description="a", backend_mappings={"fixture": False})
    outcome = resolve_batch(
        [decl],
        [_source("fixture", client_type=Client)],
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=None,
    ).outcomes[0]
    assert outcome.reason is BlockReason.NO_ATTEMPTABLE_SOURCE
    assert Client.calls == []
