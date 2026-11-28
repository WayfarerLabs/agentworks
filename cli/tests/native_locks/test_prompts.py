"""Prompts under held sets: the notice of a set acquired to prompt, and the refusal under any other."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from agentworks import output
from agentworks.errors import StateError
from agentworks.native_locks import LockRequest, native_locks, vm_entity
from agentworks.output import Role
from agentworks.secrets.base import SecretDecl
from agentworks.secrets.resolve import OutputInteractionBroker

from ._lock_support import VM, session

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pytestmark = pytest.mark.windows


class _RecordingHandler:
    """Records every emitted role and every prompt, in order."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, role: Role, message: str, level: int) -> None:
        self.events.append(f"emit:{role.name}")

    def style_status(self, text: str, style: object) -> str:
        return text

    def confirm(self, message: str, level: int, default: bool = False) -> bool:
        self.events.append("prompt")
        return True

    def choose(self, message: str, options: list[str], level: int) -> int:
        self.events.append("prompt")
        return 0

    def pause(self, message: str, level: int) -> None:
        self.events.append("prompt")

    def prompt(self, label: str, level: int, default: str | None = None) -> str:
        self.events.append("prompt")
        return "value"

    def prompt_secret(self, label: str, level: int, hint: str | None = None) -> str:
        self.events.append("prompt")
        return "secret"

    def progress(self, label: str, level: int, total: int | None = None) -> object:
        raise AssertionError("no progress expected")


@pytest.fixture
def handler() -> Iterator[_RecordingHandler]:
    previous = output.get_handler()
    recording = _RecordingHandler()
    output.set_handler(recording)  # type: ignore[arg-type]
    try:
        yield recording
    finally:
        output.set_handler(previous)


def _resolver_prompt() -> object:
    return OutputInteractionBroker([SecretDecl(name="token", description="A token")]).request_secret("token")


PROMPTS: list[Callable[[], object]] = [
    lambda: output.confirm("Continue?"),
    lambda: output.choose("Which?", ["one", "two"]),
    lambda: output.pause("Press Enter"),
    lambda: output.prompt("Name"),
    lambda: output.prompt_secret("Secret"),
    _resolver_prompt,
]
PROMPT_IDS = ["confirm", "choose", "pause", "prompt", "prompt_secret", "resolver"]

RECOVER = LockRequest(exclusive=(vm_entity(VM),))
FAMILY = LockRequest(exclusive=(session(),))


@pytest.mark.parametrize("ask", PROMPTS, ids=PROMPT_IDS)
def test_a_prompting_set_shows_its_notice_once_before_its_first_prompt(
    database: Path, handler: _RecordingHandler, ask: Callable[[], object]
) -> None:
    with native_locks(database, RECOVER, prompts=True):
        ask()
        ask()
    assert handler.events == ["emit:NOTICE", "prompt", "prompt"]


def test_each_prompting_set_shows_its_own_notice(database: Path, handler: _RecordingHandler) -> None:
    with native_locks(database, RECOVER, prompts=True):
        output.confirm("First?")
        with native_locks(database, LockRequest(exclusive=(vm_entity("other"),)), prompts=True):
            output.confirm("Second?")
            output.confirm("Third?")
    assert handler.events == ["emit:NOTICE", "prompt", "emit:NOTICE", "prompt", "prompt"]


@pytest.mark.parametrize("ask", PROMPTS, ids=PROMPT_IDS)
def test_a_prompt_under_any_other_set_raises(
    database: Path, handler: _RecordingHandler, ask: Callable[[], object]
) -> None:
    with native_locks(database, FAMILY), pytest.raises(StateError):
        ask()
    other_vm = LockRequest(exclusive=(vm_entity("other"),))
    with native_locks(database, other_vm, prompts=True), native_locks(database, FAMILY), pytest.raises(StateError):
        ask()
    assert "prompt" not in handler.events


@pytest.mark.parametrize("ask", PROMPTS, ids=PROMPT_IDS)
def test_with_no_set_held_a_prompt_shows_nothing(handler: _RecordingHandler, ask: Callable[[], object]) -> None:
    ask()
    assert handler.events == ["prompt"]


@pytest.mark.parametrize("prompts", [False, True], ids=["unmarked", "prompting"])
def test_another_invocations_set_neither_refuses_nor_notices_a_prompt(
    database: Path, handler: _RecordingHandler, prompts: bool
) -> None:
    held = threading.Event()
    done = threading.Event()

    def other_invocation() -> None:
        with native_locks(database, LockRequest(exclusive=(vm_entity("other"),)), prompts=prompts):
            held.set()
            done.wait(timeout=10)

    thread = threading.Thread(target=other_invocation)
    thread.start()
    try:
        assert held.wait(timeout=10)
        output.confirm("Continue?")
    finally:
        done.set()
        thread.join()
    assert handler.events == ["prompt"]


def test_a_confirmation_that_asks_nothing_shows_no_notice(
    database: Path, handler: _RecordingHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output, "non_interactive", lambda: True)
    with native_locks(database, RECOVER, prompts=True):
        assert output.confirm("Continue?") is False
    with native_locks(database, FAMILY):
        assert output.confirm("Continue?") is False
    assert handler.events == []
