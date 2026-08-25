"""Prompt source behavior through an explicit caller-owned broker."""

from __future__ import annotations

import pytest

from agentworks import output
from agentworks.capabilities.secret_backend import (
    BlockReason,
    IndeterminateReason,
    InteractionBroker,
    OperatorImpact,
    PreviewIndeterminate,
    ResolutionIntent,
    TtyInteractionAccess,
)
from agentworks.capabilities.secret_backend.prompt import PromptBackend, PromptSourceConfig
from agentworks.errors import UserAbort
from agentworks.resources.graph import Readiness
from agentworks.schema import CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import ResolutionBlocked, ResolutionStatus
from agentworks.secrets.preview import PreviewStatus, preview_batch
from agentworks.secrets.resolve import ActiveSource, resolve_batch


class _Broker(InteractionBroker):
    def __init__(self) -> None:
        self.names: list[str] = []

    def request_secret(self, name: str, /) -> str:
        self.names.append(name)
        return f"value:{name}"


def _source() -> ActiveSource:
    return ActiveSource(
        source=SecretSourceDecl(name="prompt", backend=CapabilityBlock.of("prompt")),
        backend_class=PromptBackend,
        config=PromptSourceConfig(name="prompt"),
        readiness=Readiness.ready(),
    )


def _decl() -> SecretDecl:
    return SecretDecl(name="token", description="Token")


def test_factory_and_context_entry_do_not_call_the_broker() -> None:
    broker = _Broker()
    context = PromptBackend.create_client(
        config=PromptSourceConfig(name="prompt"),
        intent=ResolutionIntent(),
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=broker,
    )
    client = context.__enter__()
    context.__exit__(None, None, None)
    assert client is not None
    assert broker.names == []


@pytest.mark.parametrize(
    ("impact", "access", "status", "reason", "broker_calls"),
    [
        (
            OperatorImpact.NONE,
            TtyInteractionAccess.AVAILABLE,
            PreviewStatus.INDETERMINATE,
            "operator-input-required",
            0,
        ),
        (OperatorImpact.NONE, TtyInteractionAccess.UNAVAILABLE, PreviewStatus.BLOCKED, "tty-unavailable", 0),
        (OperatorImpact.NONE, TtyInteractionAccess.DISABLED, PreviewStatus.BLOCKED, "tty-interaction-disabled", 0),
        (OperatorImpact.ALLOW, TtyInteractionAccess.AVAILABLE, PreviewStatus.AVAILABLE, None, 1),
        (OperatorImpact.ALLOW, TtyInteractionAccess.UNAVAILABLE, PreviewStatus.BLOCKED, "tty-unavailable", 0),
        (OperatorImpact.ALLOW, TtyInteractionAccess.DISABLED, PreviewStatus.BLOCKED, "tty-interaction-disabled", 0),
    ],
)
def test_preview_matrix(
    impact: OperatorImpact,
    access: TtyInteractionAccess,
    status: PreviewStatus,
    reason: str | None,
    broker_calls: int,
) -> None:
    broker = _Broker()
    preview = preview_batch(
        [_decl()],
        [_source()],
        impact=impact,
        tty_access=access,
        interaction_broker=broker,
    )["token"]
    assert preview.status is status
    assert preview.reason == reason
    assert len(broker.names) == broker_calls


def test_zero_impact_preview_with_available_tty_reports_required_operator_input() -> None:
    broker = _Broker()
    preview = preview_batch(
        [_decl()],
        [_source()],
        impact=OperatorImpact.NONE,
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=broker,
    )["token"]
    assert isinstance(preview.result, PreviewIndeterminate)
    assert preview.result.reason is IndeterminateReason.OPERATOR_INPUT_REQUIRED
    assert broker.names == []


def test_resolution_uses_broker_only_with_available_tty() -> None:
    broker = _Broker()
    batch = resolve_batch(
        [SecretDecl(name="a", description="A"), SecretDecl(name="b", description="B")],
        [_source()],
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=broker,
    )
    assert batch.complete_or_raise() == {"a": "value:a", "b": "value:b"}
    assert broker.names == ["a", "b"]


@pytest.mark.parametrize(
    ("access", "reason"),
    [
        (TtyInteractionAccess.UNAVAILABLE, BlockReason.TTY_UNAVAILABLE),
        (TtyInteractionAccess.DISABLED, BlockReason.TTY_INTERACTION_DISABLED),
    ],
)
def test_resolution_returns_exact_tty_block(access: TtyInteractionAccess, reason: BlockReason) -> None:
    broker = _Broker()
    outcome = resolve_batch(
        [_decl()],
        [_source()],
        tty_access=access,
        interaction_broker=broker,
    ).outcomes[0]
    assert outcome.status is ResolutionStatus.BLOCKED
    assert isinstance(outcome.result, ResolutionBlocked)
    assert outcome.result.reason is reason
    assert broker.names == []


def test_prompt_uses_no_global_interactivity_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output, "is_interactive", lambda: pytest.fail("typed core read global TTY state"))
    monkeypatch.setattr(output, "non_interactive", lambda: pytest.fail("typed core read global policy"))
    batch = resolve_batch(
        [_decl()],
        [_source()],
        tty_access=TtyInteractionAccess.AVAILABLE,
        interaction_broker=_Broker(),
    )
    assert batch.complete_or_raise() == {"token": "value:token"}


def test_user_abort_propagates_by_identity() -> None:
    abort = UserAbort("abort")

    class _AbortBroker(_Broker):
        def request_secret(self, name: str, /) -> str:
            raise abort

    with pytest.raises(UserAbort) as caught:
        resolve_batch(
            [_decl()],
            [_source()],
            tty_access=TtyInteractionAccess.AVAILABLE,
            interaction_broker=_AbortBroker(),
        )
    assert caught.value is abort
