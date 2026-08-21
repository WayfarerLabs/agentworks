"""Core source-first resolution and provider-aware preview behavior."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import ClassVar, Literal, cast

import pytest
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
    PreviewIntent,
    RemainingTime,
    ResolutionIntent,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.errors import UserAbort
from agentworks.resources.graph import Readiness
from agentworks.schema import AgwModel, AgwRootModel, CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import ResolutionFailed, ResolutionStatus
from agentworks.secrets.preview import PreviewStatus, preview_batch
from agentworks.secrets.resolve import ActiveSource, _SourceContextDriver, resolve_batch


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
    factory_calls: ClassVar[list[tuple[str, SecretClientIntent, TtyInteractionAccess, InteractionBroker | None]]] = []

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
        cls.factory_calls.append((source_name, intent, tty_access, interaction_broker))
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
            {
                "name": name,
                "client_type": client_type,
                "supports_tty_interaction": supports_tty,
                "factory_calls": [],
            },
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


def test_registered_conformance_and_resolution_share_the_exact_backend_name_boundary() -> None:
    from agentworks.capabilities.conformance import conformance_error
    from agentworks.capabilities.descriptor import descriptor_for

    class Client(_Client):
        resolution_results = {"a": BackendResolved("value")}

    source = _source("conforming", client_type=Client)
    assert conformance_error(descriptor_for("secret-backend"), source.backend_class) is None
    batch = resolve_batch(
        [_decl("a")],
        [source],
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"a": "value"}


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


class _Broker:
    def request_secret(self, name: str, /) -> str:
        return name


@pytest.mark.parametrize(
    ("intent", "supports_tty", "access", "broker_expected"),
    [
        (PreviewIntent(OperatorImpact.NONE), True, TtyInteractionAccess.AVAILABLE, False),
        (PreviewIntent(OperatorImpact.ALLOW), True, TtyInteractionAccess.AVAILABLE, True),
        (ResolutionIntent(), True, TtyInteractionAccess.AVAILABLE, True),
        (ResolutionIntent(), True, TtyInteractionAccess.DISABLED, False),
        (ResolutionIntent(), False, TtyInteractionAccess.AVAILABLE, False),
    ],
    ids=("preview-none", "preview-allow", "resolution-available", "resolution-disabled", "non-tty"),
)
def test_factory_receives_exact_intent_access_and_least_authority_broker(
    intent: PreviewIntent | ResolutionIntent,
    supports_tty: bool,
    access: TtyInteractionAccess,
    broker_expected: bool,
) -> None:
    class Client(_Client):
        preview_results = {"a": PreviewAvailable()}
        resolution_results = {"a": BackendResolved("value")}

    source = _source("authority", client_type=Client, supports_tty=supports_tty)
    broker = _Broker()
    if isinstance(intent, PreviewIntent):
        preview_batch(
            [_decl("a")],
            [source],
            impact=intent.impact,
            tty_access=access,
            interaction_broker=broker,
        )
    else:
        resolve_batch(
            [_decl("a")],
            [source],
            tty_access=access,
            interaction_broker=broker,
        )
    [(source_name, received_intent, received_access, received_broker)] = cast(
        "type[_Backend]", source.backend_class
    ).factory_calls
    assert source_name == "authority"
    assert received_intent == intent
    assert type(received_intent) is type(intent)
    assert received_access is access
    assert (received_broker is broker) is broker_expected


class _ExitContext(AbstractContextManager[SecretSourceClient]):
    def __init__(self, *, cleanup_error: BaseException | None = None, suppress_primary: bool = False) -> None:
        self.cleanup_error = cleanup_error
        self.suppress_primary = suppress_primary

    def __enter__(self) -> SecretSourceClient:
        return _Client()

    def __exit__(self, *args: object) -> bool:
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return self.suppress_primary


class _ProtectedExit(BaseException):
    pass


@pytest.mark.parametrize(
    "cleanup_error",
    [
        UserAbort("cleanup"),
        concurrent.futures.CancelledError(),
        KeyboardInterrupt(),
        SystemExit(),
        GeneratorExit(),
        _ProtectedExit(),
    ],
    ids=("user-abort", "cancelled", "keyboard", "system-exit", "generator-exit", "other-base"),
)
def test_cleanup_protected_exit_propagates_by_identity_without_a_primary(
    cleanup_error: BaseException,
) -> None:
    with (
        pytest.raises(type(cleanup_error)) as caught,
        _SourceContextDriver(_ExitContext(cleanup_error=cleanup_error), source_name="fixture"),
    ):
        pass
    assert caught.value is cleanup_error


def test_cleanup_preserves_primary_over_failure_and_suppression(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.secrets.resolve.output.warn", warnings.append)
    primary = RuntimeError("primary")
    for context in (
        _ExitContext(cleanup_error=RuntimeError("cleanup")),
        _ExitContext(cleanup_error=UserAbort("cleanup")),
        _ExitContext(suppress_primary=True),
    ):
        with pytest.raises(RuntimeError) as caught, _SourceContextDriver(context, source_name="fixture"):
            raise primary
        assert caught.value is primary
    assert len(warnings) == 3

    primary_abort = UserAbort("primary")
    with (
        pytest.raises(UserAbort) as caught_abort,
        _SourceContextDriver(_ExitContext(cleanup_error=RuntimeError("cleanup")), source_name="fixture"),
    ):
        raise primary_abort
    assert caught_abort.value is primary_abort
    assert len(warnings) == 4


def test_ordinary_cleanup_failure_warns_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.secrets.resolve.output.warn", warnings.append)
    with _SourceContextDriver(_ExitContext(cleanup_error=RuntimeError("native")), source_name="fixture"):
        pass
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "backend_error",
    [UserAbort("abort"), concurrent.futures.CancelledError(), KeyboardInterrupt(), GeneratorExit()],
    ids=("user-abort", "cancelled", "keyboard", "generator-exit"),
)
def test_describe_lookup_protected_exits_propagate_by_identity(backend_error: BaseException) -> None:
    source = _source("describe", client_type=_Client)

    def describe(cls: type[_Backend], secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        raise backend_error

    source.backend_class.describe_lookup = classmethod(describe)  # type: ignore[method-assign,assignment]
    with pytest.raises(type(backend_error)) as caught:
        resolve_batch(
            [_decl("a")],
            [source],
            tty_access=TtyInteractionAccess.UNAVAILABLE,
            interaction_broker=None,
        )
    assert caught.value is backend_error


def test_ordinary_describe_lookup_exception_is_text_free_backend_protocol() -> None:
    source = _source("describe", client_type=_Client)

    def describe(cls: type[_Backend], secret_name: str, mapping: BaseModel | None) -> LookupDescription:
        raise RuntimeError("provider-native sentinel")

    source.backend_class.describe_lookup = classmethod(describe)  # type: ignore[method-assign,assignment]
    outcome = resolve_batch(
        [_decl("a")],
        [source],
        tty_access=TtyInteractionAccess.UNAVAILABLE,
        interaction_broker=None,
    ).outcomes[0]
    assert outcome.reason is FailureReason.BACKEND_PROTOCOL
    assert "provider-native sentinel" not in repr(outcome)


class _SingleReadMapping(Mapping[str, BackendResolution]):
    def __init__(self, result: BackendResolution) -> None:
        self.result = result
        self.reads = 0

    def __iter__(self) -> Iterator[str]:
        return iter(("a",))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> BackendResolution:
        assert key == "a"
        self.reads += 1
        if self.reads != 1:
            raise RuntimeError("backend mapping was consumed after snapshot")
        return self.result


def test_backend_mapping_is_snapshotted_once_before_core_consumption() -> None:
    returned = _SingleReadMapping(BackendResolved("value"))

    class Client(_Client):
        def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
            return returned

    batch = resolve_batch(
        [_decl("a")],
        [_source("snapshot", client_type=Client)],
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"a": "value"}
    assert returned.reads == 1


def test_lazy_mapping_failure_becomes_backend_protocol() -> None:
    class LazyFailure(Mapping[str, BackendResolution]):
        def __iter__(self) -> Iterator[str]:
            raise RuntimeError("lazy provider text")

        def __len__(self) -> int:
            return 1

        def __getitem__(self, key: str) -> BackendResolution:
            raise AssertionError("iteration must fail first")

    class Client(_Client):
        def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
            return LazyFailure()

    outcome = resolve_batch(
        [_decl("a")],
        [_source("lazy", client_type=Client)],
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    ).outcomes[0]
    assert outcome.reason is FailureReason.BACKEND_PROTOCOL
    assert "lazy provider text" not in repr(outcome)


def test_mapping_snapshot_preserves_user_abort_by_identity() -> None:
    abort = UserAbort("abort")

    class ProtectedMapping(Mapping[str, BackendResolution]):
        def __iter__(self) -> Iterator[str]:
            raise abort

        def __len__(self) -> int:
            return 1

        def __getitem__(self, key: str) -> BackendResolution:
            raise AssertionError("iteration must abort first")

    class Client(_Client):
        def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
            return ProtectedMapping()

    with pytest.raises(UserAbort) as caught:
        resolve_batch(
            [_decl("a")],
            [_source("protected", client_type=Client)],
            tty_access=TtyInteractionAccess.DISABLED,
            interaction_broker=None,
        )
    assert caught.value is abort


@pytest.mark.parametrize(
    "result",
    [
        BackendBlocked(BlockReason.SOURCE_NOT_READY),
        BackendFailed(FailureReason.BACKEND_PROTOCOL),
    ],
    ids=("core-only-block", "core-only-failure"),
)
def test_backend_cannot_forge_core_owned_reasons(result: BackendResolution) -> None:
    class Client(_Client):
        resolution_results = {"a": result}

    outcome = resolve_batch(
        [_decl("a")],
        [_source("forged", client_type=Client, supports_tty=True)],
        tty_access=TtyInteractionAccess.UNAVAILABLE,
        interaction_broker=None,
    ).outcomes[0]
    assert outcome.reason is FailureReason.BACKEND_PROTOCOL


def test_forged_resolved_result_is_revalidated_before_value_copy() -> None:
    forged = object.__new__(BackendResolved)
    object.__setattr__(forged, "value", "must-not-escape\0")

    class Client(_Client):
        resolution_results = {"a": forged}

    batch = resolve_batch(
        [_decl("a")],
        [_source("forged", client_type=Client)],
        tty_access=TtyInteractionAccess.DISABLED,
        interaction_broker=None,
    )
    assert batch.outcomes[0].reason is FailureReason.BACKEND_PROTOCOL
    assert "must-not-escape" not in repr(batch)


def test_duplicate_declarations_use_the_first_mapping_in_preview_and_resolution() -> None:
    class Client(_Client):
        preview_results = {"a": PreviewAvailable()}
        resolution_results = {"a": BackendResolved("unused")}
        calls = []

    first = SecretDecl(name="a", description="first", backend_mappings={"duplicates": False})
    second = SecretDecl(name="a", description="second")
    preview = preview_batch(
        [first, second],
        [_source("duplicates", client_type=Client)],
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.UNAVAILABLE,
        interaction_broker=None,
    )["a"]
    outcome = resolve_batch(
        [first, second],
        [_source("duplicates", client_type=Client)],
        tty_access=TtyInteractionAccess.UNAVAILABLE,
        interaction_broker=None,
    ).outcomes[0]
    assert preview.status is PreviewStatus.BLOCKED
    assert outcome.reason is BlockReason.NO_ATTEMPTABLE_SOURCE
    assert Client.calls == []
