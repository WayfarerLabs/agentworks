"""Prompt source behavior through an explicit caller-owned broker."""

from __future__ import annotations

import inspect
import sys
from contextlib import contextmanager
from typing import Any

import pytest

from agentworks import output
from agentworks.capabilities.secret_backend.client import InteractionBroker, SecretLookupRequest
from agentworks.capabilities.secret_backend.prompt import PromptBackend, PromptSourceConfig
from agentworks.errors import StateError, UserAbort
from agentworks.resources.graph import Readiness
from agentworks.schema import CapabilityBlock
from agentworks.secrets import SecretDecl, SecretSourceDecl
from agentworks.secrets.resolve import (
    ActiveSource,
    CompletionPolicy,
    InteractionPolicy,
    ResolutionCategory,
    ResolutionDetail,
    ResolutionPolicy,
    resolve_batch,
)


@contextmanager
def _interrupt_return_line(function: Any) -> Any:
    lines, first_line = inspect.getsourcelines(function)
    matches = [
        first_line + index for index, line in enumerate(lines) if line.rstrip("\n") == "            return resolved"
    ]
    assert len(matches) == 1
    target_line = matches[0]
    target_code = function.__code__

    def trace(frame: Any, event: str, argument: object) -> Any:
        del argument
        if frame.f_code is target_code and event == "line" and frame.f_lineno == target_line:
            sys.settrace(None)
            raise KeyboardInterrupt
        return trace

    sys.settrace(trace)
    try:
        yield
    finally:
        sys.settrace(None)


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
        policy=ResolutionPolicy(InteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
        interaction_broker=broker,
    )
    assert batch.complete_or_raise() == {"a": "value:a", "b": "value:b"}
    assert broker.names == ["a", "b"]


def test_refusal_constructs_nothing_and_records_typed_outcome() -> None:
    broker = _Broker()
    batch = resolve_batch(
        [SecretDecl(name="x", description="X")],
        [_source()],
        policy=ResolutionPolicy(InteractionPolicy.REFUSE, CompletionPolicy.COMPLETE),
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
        policy=ResolutionPolicy(InteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
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
            policy=ResolutionPolicy(InteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
            interaction_broker=None,
        )


def test_prompt_uses_no_tty_or_global_interactivity_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output, "is_interactive", lambda: pytest.fail("typed core read global TTY state"))
    monkeypatch.setattr(output, "non_interactive", lambda: pytest.fail("typed core read global policy"))
    broker = _Broker()
    batch = resolve_batch(
        [SecretDecl(name="x", description="X")],
        [_source()],
        policy=ResolutionPolicy(InteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
        interaction_broker=broker,
    )
    assert batch.complete_or_raise() == {"x": "value:x"}


def test_prompt_abort_propagates_without_retaining_earlier_answers() -> None:
    class _AbortBroker(_Broker):
        def request_secret(self, name: str, /) -> str:
            if name == "b":
                raise KeyboardInterrupt
            return "sentinel-first-value"

    with pytest.raises(KeyboardInterrupt) as caught:
        resolve_batch(
            [SecretDecl(name="a", description="A"), SecretDecl(name="b", description="B")],
            [_source()],
            policy=ResolutionPolicy(InteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
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
            policy=ResolutionPolicy(InteractionPolicy.ALLOW, CompletionPolicy.COMPLETE),
            interaction_broker=_AbortBroker(),
        )

    assert caught.value is abort


def test_exact_success_return_interrupt_clears_prompt_value_from_traceback_locals() -> None:
    class _ReturnBroker(_Broker):
        def request_secret(self, name: str, /) -> str:
            del name
            return "sentinel-prompt-return-value"

    context = PromptBackend.create_client(
        source_name="prompt",
        config=PromptSourceConfig(name="prompt"),
        interaction_broker=_ReturnBroker(),
        remaining_time=lambda: None,
    )
    client = context.__enter__()
    try:
        with pytest.raises(KeyboardInterrupt) as caught, _interrupt_return_line(type(client).resolve):
            client.resolve((SecretLookupRequest(name="token", mapping=None),), remaining_time=lambda: None)
    finally:
        context.__exit__(None, None, None)

    local_text: list[str] = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("agentworks."):
            local_text.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    assert "sentinel-prompt-return-value" not in "\n".join(local_text)
