"""Prompt source behavior through an explicit caller-owned broker."""

from __future__ import annotations

import pytest

from agentworks import output
from agentworks.capabilities.secret_backend.client import InteractionBroker
from agentworks.capabilities.secret_backend.prompt import PromptBackend, PromptSourceConfig
from agentworks.errors import StateError, UserAbort
from agentworks.resources.graph import Readiness
from agentworks.schema import CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.outcomes import ResolutionCategory, ResolutionDetail
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.secrets.resolve import (
    ActiveSource,
    CompletionPolicy,
    ResolutionPolicy,
    resolve_batch,
)


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


def test_prompt_requires_explicit_allow_and_uses_name_only_broker() -> None:
    broker = _Broker()
    batch = resolve_batch(
        [SecretDecl(name="a", description="A"), SecretDecl(name="b", description="B")],
        [_source()],
        policy=ResolutionPolicy(TtyInteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
        interaction_broker=broker,
    )
    assert batch.complete_or_raise() == {"a": "value:a", "b": "value:b"}
    assert broker.names == ["a", "b"]


def test_refusal_constructs_nothing_and_records_typed_outcome() -> None:
    broker = _Broker()
    batch = resolve_batch(
        [SecretDecl(name="x", description="X")],
        [_source()],
        policy=ResolutionPolicy(TtyInteractionPolicy.REFUSE, CompletionPolicy.COMPLETE),
        interaction_broker=broker,
    )
    assert broker.names == []
    assert batch.outcomes[0].category is ResolutionCategory.REFUSED_INTERACTION
    assert batch.outcomes[0].detail is ResolutionDetail.INTERACTION_REFUSED


def test_false_mapping_opts_out_before_broker() -> None:
    broker = _Broker()
    batch = resolve_batch(
        [SecretDecl(name="x", description="X", backend_mappings={"prompt": False})],
        [_source()],
        policy=ResolutionPolicy(TtyInteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
        interaction_broker=broker,
    )
    assert broker.names == []
    assert batch.outcomes[0].detail is ResolutionDetail.NO_ATTEMPTABLE_SOURCE


def test_prompt_has_no_static_identifier() -> None:
    assert _source().describe_lookup(SecretDecl(name="x", description="X")) is None


def test_allowed_prompt_without_broker_is_state_error() -> None:
    with pytest.raises(StateError, match="interaction broker"):
        resolve_batch(
            [SecretDecl(name="x", description="X")],
            [_source()],
            policy=ResolutionPolicy(TtyInteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
            interaction_broker=None,
        )


def test_prompt_uses_no_tty_or_global_interactivity_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output, "is_interactive", lambda: pytest.fail("typed core read global TTY state"))
    monkeypatch.setattr(output, "non_interactive", lambda: pytest.fail("typed core read global policy"))
    broker = _Broker()
    batch = resolve_batch(
        [SecretDecl(name="x", description="X")],
        [_source()],
        policy=ResolutionPolicy(TtyInteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
        interaction_broker=broker,
    )
    assert batch.complete_or_raise() == {"x": "value:x"}


def test_prompt_abort_does_not_expose_earlier_answers_in_exception() -> None:
    class _AbortBroker(_Broker):
        def request_secret(self, name: str, /) -> str:
            if name == "b":
                raise KeyboardInterrupt
            return "sentinel-first-value"

    with pytest.raises(KeyboardInterrupt) as caught:
        resolve_batch(
            [SecretDecl(name="a", description="A"), SecretDecl(name="b", description="B")],
            [_source()],
            policy=ResolutionPolicy(TtyInteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
            interaction_broker=_AbortBroker(),
        )
    assert "sentinel-first-value" not in repr(caught.value)


def test_prompt_broker_user_abort_propagates_by_identity() -> None:
    abort = UserAbort("sentinel-user-abort")

    class _AbortBroker(_Broker):
        def request_secret(self, name: str, /) -> str:
            del name
            raise abort

    with pytest.raises(UserAbort) as caught:
        resolve_batch(
            [SecretDecl(name="token", description="Token")],
            [_source()],
            policy=ResolutionPolicy(TtyInteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
            interaction_broker=_AbortBroker(),
        )

    assert caught.value is abort
