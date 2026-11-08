"""What ``attach`` does around the terminal half: the guard, and the notice that must follow it."""

from __future__ import annotations

import contextlib
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks import output
from agentworks.execution import attach as attach_module
from agentworks.execution.requests import LoginShell

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.execution.conformance.lanes import LocalLane


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="agw-launch needs Linux")


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    log: list[tuple[str, Any]] = []

    @contextlib.contextmanager
    def guard() -> Iterator[SimpleNamespace]:
        handle = SimpleNamespace(clean_exit=False)
        log.append(("enter", None))
        try:
            yield handle
        finally:
            log.append(("exit", handle.clean_exit))

    monkeypatch.setattr(attach_module, "guarded_terminal", guard)
    monkeypatch.setattr(output, "warn", lambda message: log.append(("warn", message)))
    monkeypatch.setattr(output, "detail", lambda message: log.append(("detail", message)))
    return log


def test_a_dropped_connection_is_explained_after_the_guard_has_closed(
    local_lane: LocalLane, events: list[tuple[str, Any]]
) -> None:
    with local_lane.terminal(behavior=lambda argv, half: 255):
        assert local_lane.access.attach(LoginShell(cwd=local_lane.workdir)) == 255

    kinds = [kind for kind, _ in events]
    assert kinds == ["enter", "exit", "warn", "detail"]  # I6: the guard's reset would wipe an earlier notice
    assert events[1] == ("exit", False)


@pytest.mark.parametrize(("code", "clean"), [(0, True), (1, False), (130, False)])
def test_only_a_zero_exit_skips_the_full_terminal_reset(
    local_lane: LocalLane, events: list[tuple[str, Any]], code: int, clean: bool
) -> None:
    with local_lane.terminal(behavior=lambda argv, half: code):
        assert local_lane.access.attach(LoginShell(cwd=local_lane.workdir)) == code

    assert events == [("enter", None), ("exit", clean)]


def test_the_terminal_is_restored_when_the_terminal_half_is_interrupted(
    local_lane: LocalLane, events: list[tuple[str, Any]]
) -> None:
    def interrupted(argv: list[str], half: object) -> int:
        raise KeyboardInterrupt

    token = local_lane.handover_token()
    try:
        with local_lane.terminal(behavior=interrupted), pytest.raises(KeyboardInterrupt):
            local_lane.access.attach(LoginShell(cwd=local_lane.workdir))

        assert events == [("enter", None), ("exit", False)]  # I21
    finally:
        # The pump kills the parking client at once here, and in this lane that kills the launcher
        # itself; over ssh the launcher sees its stdin close and removes the directory.
        local_lane.remove_dir(f"/tmp/agw-handover-{token}")


def test_the_terminal_half_runs_the_handover_stage_and_not_the_request(local_lane: LocalLane) -> None:
    secret = "sekrit-env-value-5e"
    with local_lane.terminal(behavior=lambda argv, half: 0) as terminal:
        local_lane.access.attach(LoginShell(cwd=local_lane.workdir, env={"TOKEN": secret}))

    assert terminal.calls == 1
    assert not any(secret in argument for argument in terminal.argv)
    assert "/tmp/agw-handover-" in terminal.argv[-1]


def test_the_login_shell_gets_the_requested_environment_without_it_touching_a_command_line(
    local_lane: LocalLane,
) -> None:
    secret = "sekrit-env-value-5e"
    with local_lane.terminal(b'echo "token=$TOKEN"\necho "home=$HOME"\nexit 0\n') as terminal:
        code = local_lane.access.attach(LoginShell(cwd=local_lane.workdir, env={"TOKEN": secret, "HOME": "/tmp"}))

    assert code == 0
    assert terminal.stdout.splitlines()[:2] == [f"token={secret}".encode(), b"home=/tmp"]
    assert not any(secret in argument for argument in terminal.argv)
