"""Ordered typed secret-source resolution semantics."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import ClassVar

import pytest

from agentworks.capabilities.secret_backend import InteractionChannel
from agentworks.capabilities.secret_backend.client import (
    InteractionBroker,
    RemainingTime,
    SecretClientFailure,
    SecretClientFailureKind,
    SecretClientRemediation,
    SecretSourceClient,
)
from agentworks.capabilities.secret_backend.prompt import PromptBackend, PromptSourceConfig
from agentworks.resources.graph import Readiness
from agentworks.schema import AgwModel, CapabilityBlock
from agentworks.secrets import SecretDecl
from agentworks.secrets.outcomes import ResolutionCategory, ResolutionDetail
from agentworks.secrets.policy import InteractionPolicy
from agentworks.secrets.resolve import (
    ActiveSource,
    CompletionPolicy,
    ResolutionPolicy,
    resolve_batch,
)
from agentworks.secrets.sources import SecretSourceDecl
from tests.secrets.test_resolution_lifecycle import _Backend, _source


def _policy(*, partial: bool = False) -> ResolutionPolicy:
    return ResolutionPolicy(
        interaction=InteractionPolicy.REFUSE,
        completion=CompletionPolicy.PARTIAL if partial else CompletionPolicy.COMPLETE,
    )


class _First(_Backend):
    events: ClassVar[list[str]] = []
    values: ClassVar[dict[str, str]] = {}
    failure: ClassVar[BaseException | None] = None


class _Second(_Backend):
    events: ClassVar[list[str]] = []
    values: ClassVar[dict[str, str]] = {}
    failure: ClassVar[BaseException | None] = None


def _reset() -> None:
    for backend in (_First, _Second):
        backend.events = []
        backend.values = {}
        backend.failure = None


def test_first_resolved_source_wins_and_later_source_is_lazy() -> None:
    _reset()
    _First.values = {"x": "first"}
    _Second.values = {"x": "second"}
    batch = resolve_batch(
        [SecretDecl(name="x", description="x")],
        [_source(name="first", backend_class=_First), _source(name="second", backend_class=_Second)],
        policy=_policy(),
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"x": "first"}
    assert _Second.events == []


def test_soft_miss_falls_through_in_request_order() -> None:
    _reset()
    _Second.values = {"x": "second"}
    batch = resolve_batch(
        [SecretDecl(name="x", description="x")],
        [_source(name="first", backend_class=_First), _source(name="second", backend_class=_Second)],
        policy=_policy(),
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"x": "second"}
    assert _First.events == ["factory", "enter", "prepare", "resolve", "exit"]
    assert _Second.events == ["factory", "enter", "prepare", "resolve", "exit"]


def test_hard_failure_halts_attempted_secret_but_unrelated_secret_continues() -> None:
    _reset()
    _First.failure = SecretClientFailure(
        kind=SecretClientFailureKind.HARD_MAPPING,
        remediation=SecretClientRemediation.CHECK_MAPPING,
    )
    _Second.values = {"other": "ok"}
    mapped = SecretDecl(name="mapped", description="mapped")
    other = SecretDecl(name="other", description="other", backend_mappings={"first": False})
    batch = resolve_batch(
        [mapped, other],
        [_source(name="first", backend_class=_First), _source(name="second", backend_class=_Second)],
        policy=_policy(partial=True),
        interaction_broker=None,
    )
    assert batch.outcomes[0].detail is ResolutionDetail.HARD_MAPPING
    assert batch.outcomes[1].category is ResolutionCategory.RESOLVED


def test_false_opt_out_is_keyed_by_source_name() -> None:
    _reset()
    _First.values = {"x": "wrong"}
    _Second.values = {"x": "right"}
    decl = SecretDecl(name="x", description="x", backend_mappings={"first": False})
    batch = resolve_batch(
        [decl],
        [_source(name="first", backend_class=_First), _source(name="second", backend_class=_Second)],
        policy=_policy(),
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"x": "right"}
    assert _First.events == []


def test_client_mapping_iteration_order_cannot_reorder_outcomes() -> None:
    _reset()
    _First.values = {"b": "b", "a": "a"}
    batch = resolve_batch(
        [SecretDecl(name="a", description="a"), SecretDecl(name="b", description="b")],
        [_source(name="first", backend_class=_First)],
        policy=_policy(),
        interaction_broker=None,
    )
    assert [outcome.name for outcome in batch.outcomes] == ["a", "b"]


@pytest.mark.parametrize(
    ("kind", "detail"),
    [
        (SecretClientFailureKind.HARD_MAPPING, ResolutionDetail.HARD_MAPPING),
        (SecretClientFailureKind.AUTHENTICATION, ResolutionDetail.AUTHENTICATION),
        (SecretClientFailureKind.CONNECTIVITY, ResolutionDetail.CONNECTIVITY),
        (SecretClientFailureKind.EXTERNAL, ResolutionDetail.EXTERNAL),
    ],
)
def test_every_typed_hard_failure_halts_the_attempted_secret(
    kind: SecretClientFailureKind,
    detail: ResolutionDetail,
) -> None:
    _reset()
    remediation = {
        SecretClientFailureKind.HARD_MAPPING: SecretClientRemediation.CHECK_MAPPING,
        SecretClientFailureKind.AUTHENTICATION: SecretClientRemediation.SIGN_IN,
        SecretClientFailureKind.CONNECTIVITY: SecretClientRemediation.CHECK_CONNECTIVITY,
        SecretClientFailureKind.EXTERNAL: SecretClientRemediation.RETRY,
    }[kind]
    _First.failure = SecretClientFailure(kind=kind, remediation=remediation)
    _Second.values = {"token": "must-not-fall-through"}
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [_source(name="first", backend_class=_First), _source(name="second", backend_class=_Second)],
        policy=_policy(partial=True),
        interaction_broker=None,
    )
    assert batch.outcomes[0].detail is detail
    assert _Second.events == []


def test_not_ready_source_falls_through_without_construction() -> None:
    _reset()
    _Second.values = {"token": "winner"}
    batch = resolve_batch(
        [SecretDecl(name="token", description="token")],
        [
            _source(name="first", backend_class=_First, ready=False),
            _source(name="second", backend_class=_Second),
        ],
        policy=_policy(),
        interaction_broker=None,
    )
    assert batch.complete_or_raise() == {"token": "winner"}
    assert _First.events == []


class _Interactive(_Second):
    # Out-of-band is the generic interactive channel here: these tests cover
    # consent gating, not terminal availability.
    interaction_channel: ClassVar[InteractionChannel] = InteractionChannel.OUT_OF_BAND


class _NeverAttempts(_Second):
    @classmethod
    def would_attempt(cls, secret_name: str, *, mapping_present: bool) -> bool:
        return False


def test_final_evidence_precedence_is_refused_then_soft_miss_then_not_ready() -> None:
    _reset()
    secret = SecretDecl(name="token", description="token")
    refused = resolve_batch(
        [secret],
        [
            _source(name="first", backend_class=_First),
            _source(name="second", backend_class=_Interactive),
        ],
        policy=_policy(partial=True),
        interaction_broker=None,
    )
    assert refused.outcomes[0].detail is ResolutionDetail.NON_INTERACTIVE_REFUSED
    assert refused.outcomes[0].source == "second"

    soft = resolve_batch(
        [secret],
        [
            _source(name="first", backend_class=_First),
            _source(name="second", backend_class=_Second, ready=False),
        ],
        policy=_policy(partial=True),
        interaction_broker=None,
    )
    assert soft.outcomes[0].detail is ResolutionDetail.SOFT_MISS
    assert soft.outcomes[0].source == "first"

    not_ready = resolve_batch(
        [secret],
        [_source(name="second", backend_class=_Second, ready=False)],
        policy=_policy(partial=True),
        interaction_broker=None,
    )
    assert not_ready.outcomes[0].detail is ResolutionDetail.SOURCE_NOT_READY
    assert not_ready.outcomes[0].source == "second"


def test_empty_chain_and_non_attempting_chain_are_distinct() -> None:
    secret = SecretDecl(name="token", description="token")
    empty = resolve_batch([secret], [], policy=_policy(partial=True), interaction_broker=None)
    inert = resolve_batch(
        [secret],
        [_source(name="inert", backend_class=_NeverAttempts)],
        policy=_policy(partial=True),
        interaction_broker=None,
    )
    assert empty.outcomes[0].detail is ResolutionDetail.NO_ACTIVE_SOURCE
    assert inert.outcomes[0].detail is ResolutionDetail.NO_ATTEMPTABLE_SOURCE


class _Broker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def request_secret(self, name: str, /) -> str:
        self.calls.append(name)
        return f"prompted-{name}"


class _PromptProbe(PromptBackend):
    factories: ClassVar[int] = 0

    @classmethod
    def create_client(
        cls,
        *,
        source_name: str,
        config: AgwModel,
        interaction_broker: InteractionBroker | None,
        remaining_time: RemainingTime,
    ) -> AbstractContextManager[SecretSourceClient]:
        cls.factories += 1
        return super().create_client(
            source_name=source_name,
            config=config,
            interaction_broker=interaction_broker,
            remaining_time=remaining_time,
        )


def _prompt_source() -> ActiveSource:
    return ActiveSource(
        source=SecretSourceDecl(name="prompt", backend=CapabilityBlock.of("prompt")),
        backend_class=_PromptProbe,
        config=PromptSourceConfig(name="prompt"),
        readiness=Readiness.ready(),
    )


def test_complete_mode_dooms_before_prompt_factory_or_broker() -> None:
    _reset()
    _PromptProbe.factories = 0
    broker = _Broker()
    doomed = SecretDecl(name="doomed", description="doomed", backend_mappings={"prompt": False})
    independent = SecretDecl(name="independent", description="independent")
    batch = resolve_batch(
        [doomed, independent],
        [_source(name="first", backend_class=_First), _prompt_source()],
        policy=ResolutionPolicy(interaction=InteractionPolicy.ALLOW, completion=CompletionPolicy.COMPLETE),
        interaction_broker=broker,
    )
    assert [outcome.detail for outcome in batch.outcomes] == [
        ResolutionDetail.SOFT_MISS,
        ResolutionDetail.BATCH_DOOMED,
    ]
    assert _PromptProbe.factories == 0
    assert broker.calls == []


def test_partial_mode_resolves_independent_secret_instead_of_dooming() -> None:
    _reset()
    _PromptProbe.factories = 0
    broker = _Broker()
    doomed = SecretDecl(name="doomed", description="doomed", backend_mappings={"prompt": False})
    independent = SecretDecl(name="independent", description="independent")
    batch = resolve_batch(
        [doomed, independent],
        [_source(name="first", backend_class=_First), _prompt_source()],
        policy=ResolutionPolicy(interaction=InteractionPolicy.ALLOW, completion=CompletionPolicy.PARTIAL),
        interaction_broker=broker,
    )
    assert [outcome.detail for outcome in batch.outcomes] == [
        ResolutionDetail.SOFT_MISS,
        ResolutionDetail.RESOLVED,
    ]
    assert _PromptProbe.factories == 1
    assert broker.calls == ["independent"]


def test_complete_doom_applies_to_interactive_plugin_without_a_broker() -> None:
    _reset()
    doomed = SecretDecl(name="doomed", description="doomed", backend_mappings={"interactive": False})
    independent = SecretDecl(name="independent", description="independent")
    batch = resolve_batch(
        [doomed, independent],
        [
            _source(name="first", backend_class=_First),
            _source(name="interactive", backend_class=_Interactive),
        ],
        policy=ResolutionPolicy(interaction=InteractionPolicy.ALLOW, completion=CompletionPolicy.COMPLETE),
        interaction_broker=None,
    )
    assert batch.outcomes[1].detail is ResolutionDetail.BATCH_DOOMED
    assert _Interactive.events == []
