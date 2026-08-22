"""Complete-batch doom and the explicit partial-reveal carveout."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import ClassVar, cast

import pytest
from pydantic import BaseModel

from agentworks.capabilities.secret_backend import (
    BackendFailed,
    BackendMissing,
    BackendResolution,
    BackendResolved,
    BlockReason,
    FailureReason,
    InteractionBroker,
    LookupDescription,
    LookupDisposition,
    SecretClientIntent,
    SecretLookupRequest,
    SecretSourceClient,
    TtyInteractionAccess,
)
from agentworks.capabilities.secret_backend.base import SecretBackend
from agentworks.resources.graph import Readiness
from agentworks.schema import AgwModel, AgwRootModel, CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import ResolutionBlocked, ResolutionFailed, ResolutionMissing, ResolutionStatus
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolve import ActiveSource, resolve_batch, resolve_partial_for_reveal


class _Config(AgwModel):
    name: str


class _Mapping(AgwRootModel[str]):
    pass


class _Client:
    results: ClassVar[dict[str, BackendResolution]] = {}
    calls: ClassVar[list[tuple[str, ...]]] = []

    def __init__(self, broker: InteractionBroker | None) -> None:
        self._broker = broker

    def preview(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, object]:
        raise AssertionError("actual-resolution fixture previewed")

    def resolve(self, requests: tuple[SecretLookupRequest, ...]) -> Mapping[str, BackendResolution]:
        self.calls.append(tuple(request.name for request in requests))
        if self._broker is not None:
            for request in requests:
                self._broker.request_secret(request.name)
        return {request.name: self.results[request.name] for request in requests}


class _Backend(SecretBackend):
    name = "fixture"
    description = "fixture"
    contract_version = 1
    config_model = _Config
    mapping_model = _Mapping
    supports_tty_interaction = False
    client_type: ClassVar[type[_Client]] = _Client
    factory_calls: ClassVar[int] = 0

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
        config: AgwModel,
        intent: SecretClientIntent,
        tty_access: TtyInteractionAccess,
        interaction_broker: InteractionBroker | None,
    ) -> AbstractContextManager[SecretSourceClient]:
        cls.factory_calls += 1
        return nullcontext(cast("SecretSourceClient", cls.client_type(interaction_broker)))


def _source(
    name: str,
    client_type: type[_Client],
    *,
    supports_tty: bool = False,
    ready: bool = True,
    disabled_plugin: str | None = None,
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
                "factory_calls": 0,
            },
        ),
    )
    return ActiveSource(
        source=SecretSourceDecl(name=name, backend=CapabilityBlock.of(name)),
        backend_class=backend,
        config=_Config(name=name),
        readiness=Readiness.ready() if ready else Readiness.blocked("fixture unavailable"),
        disabled_backend_plugin=disabled_plugin,
    )


def _decl(name: str) -> SecretDecl:
    return SecretDecl(name=name, description=name)


class _Broker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def request_secret(self, name: str, /) -> str:
        self.calls.append(name)
        return "unused"


def test_hard_failure_dooms_complete_sibling_before_later_provider_and_broker() -> None:
    class First(_Client):
        results = {"a": BackendFailed(FailureReason.LOOKUP_REJECTED), "b": BackendMissing()}
        calls = []

    class Later(_Client):
        results = {"b": BackendResolved("second-b")}
        calls = []

    later = _source("later", Later, supports_tty=True)
    broker = _Broker()
    batch = resolve_batch(
        [_decl("a"), _decl("b")],
        [_source("first", First), later],
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=broker,
    )

    assert isinstance(batch.outcomes[0].result, ResolutionFailed)
    assert batch.outcomes[0].result.reason is FailureReason.LOOKUP_REJECTED
    assert isinstance(batch.outcomes[1].result, ResolutionBlocked)
    assert batch.outcomes[1].result.reason is BlockReason.BATCH_DOOMED
    assert batch.outcomes[1].source is None
    assert cast("type[_Backend]", later.backend_class).factory_calls == 0
    assert Later.calls == []
    assert broker.calls == []


def test_partial_reveal_continues_independent_name_after_hard_failure() -> None:
    class First(_Client):
        results = {"a": BackendFailed(FailureReason.LOOKUP_REJECTED), "b": BackendMissing()}
        calls = []

    class Later(_Client):
        results = {"b": BackendResolved("second-b")}
        calls = []

    partial = resolve_partial_for_reveal(
        [_decl("a"), _decl("b")],
        [_source("first", First), _source("later", Later)],
        interaction=TtyInteractionPolicy.REFUSE,
    )

    assert tuple(outcome.status for outcome in partial.outcomes) == (
        ResolutionStatus.FAILED,
        ResolutionStatus.RESOLVED,
    )
    assert partial.values == {"b": "second-b"}
    assert Later.calls == [("b",)]


def test_static_no_remaining_candidate_dooms_sibling_before_later_provider() -> None:
    class First(_Client):
        results = {"a": BackendMissing(), "b": BackendMissing()}
        calls = []

    class Later(_Client):
        results = {"b": BackendResolved("second-b")}
        calls = []

    declarations = [
        SecretDecl(name="a", description="a", backend_mappings={"later": False}),
        _decl("b"),
    ]
    later = _source("later", Later, supports_tty=True)
    broker = _Broker()
    batch = resolve_batch(
        declarations,
        [_source("first", First), later],
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=broker,
    )

    assert isinstance(batch.outcomes[0].result, ResolutionMissing)
    assert batch.outcomes[0].source == "first"
    assert isinstance(batch.outcomes[1].result, ResolutionBlocked)
    assert batch.outcomes[1].result.reason is BlockReason.BATCH_DOOMED
    assert batch.outcomes[1].source is None
    assert cast("type[_Backend]", later.backend_class).factory_calls == 0
    assert Later.calls == []
    assert broker.calls == []


@pytest.mark.parametrize(
    "tty_access",
    [TtyInteractionAccess.UNAVAILABLE, TtyInteractionAccess.DISABLED],
)
def test_tty_access_never_changes_static_remaining_viability(
    tty_access: TtyInteractionAccess,
) -> None:
    class First(_Client):
        results = {"a": BackendMissing(), "b": BackendMissing()}
        calls = []

    class Later(_Client):
        results = {"a": BackendResolved("second-a"), "b": BackendResolved("second-b")}
        calls = []

    batch = resolve_batch(
        [_decl("a"), _decl("b")],
        [_source("first", First), _source("later", Later, supports_tty=True)],
        tty_access=tty_access,
        interaction_broker=None,
    )

    assert batch.complete_or_raise() == {"a": "second-a", "b": "second-b"}
    assert Later.calls == [("a", "b")]


@pytest.mark.parametrize(
    ("ready", "disabled_plugin", "reason"),
    [
        (False, None, BlockReason.SOURCE_NOT_READY),
        (True, "fixture-plugin", BlockReason.BACKEND_PLUGIN_DISABLED),
    ],
    ids=("not-ready", "plugin-disabled"),
)
def test_static_unavailable_candidate_stops_before_an_independent_provider(
    ready: bool,
    disabled_plugin: str | None,
    reason: BlockReason,
) -> None:
    class Provider(_Client):
        results = {"b": BackendResolved("provider-b")}
        calls = []

    class Blocked(_Client):
        results = {"a": BackendResolved("blocked-a")}
        calls = []

    provider = _source("provider", Provider)
    blocked = _source("blocked", Blocked, ready=ready, disabled_plugin=disabled_plugin)
    batch = resolve_batch(
        [
            SecretDecl(name="a", description="a", backend_mappings={"provider": False}),
            SecretDecl(name="b", description="b", backend_mappings={"blocked": False}),
        ],
        [provider, blocked],
        tty_access=TtyInteractionAccess.UNAVAILABLE,
        interaction_broker=None,
    )

    assert isinstance(batch.outcomes[0].result, ResolutionBlocked)
    assert batch.outcomes[0].result.reason is reason
    assert batch.outcomes[0].source == "blocked"
    assert isinstance(batch.outcomes[1].result, ResolutionBlocked)
    assert batch.outcomes[1].result.reason is BlockReason.BATCH_DOOMED
    assert cast("type[_Backend]", provider.backend_class).factory_calls == 0
    assert Provider.calls == []
