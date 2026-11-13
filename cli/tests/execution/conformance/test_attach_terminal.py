"""What ``attach`` does around the terminal half: the guard, and the notice that must follow it."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks import output
from agentworks.execution import attach as attach_module
from agentworks.execution.errors import RequestRefused
from agentworks.execution.requests import LoginShell, TerminalCommand

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.execution.conformance.lanes import LocalLane


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
    monkeypatch.setattr(attach_module, "emit_clear", lambda: log.append(("clear", None)))
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


def test_a_requested_clear_follows_the_guard_and_precedes_the_notice(
    local_lane: LocalLane, events: list[tuple[str, Any]]
) -> None:
    with local_lane.terminal(behavior=lambda argv, half: 255):
        local_lane.access.attach(LoginShell(cwd=local_lane.workdir), clear_screen_on_exit=True)

    assert [kind for kind, _ in events] == ["enter", "exit", "clear", "warn", "detail"]


# -- T1: a terminal command --------------------------------------------------------------


def test_a_terminal_command_runs_its_argv_from_path_with_the_requested_environment(local_lane: LocalLane) -> None:
    secret = "sekrit-env-value-5e"
    command = TerminalCommand(("sh", "-c", 'echo "token=$TOKEN"; pwd'), cwd=local_lane.workdir, env={"TOKEN": secret})
    with local_lane.terminal() as terminal:
        code = local_lane.access.attach(command)

    assert code == 0
    assert terminal.stdout.splitlines()[:2] == [f"token={secret}".encode(), local_lane.workdir.encode()]
    assert not any(secret in argument for argument in terminal.argv)


@pytest.mark.parametrize(
    ("argv", "status", "code"),
    [(("agw-no-such-command-5e",), 127, "exec ENOENT"), (("/dev/null",), 126, "exec EACCES")],
)
def test_a_terminal_command_that_cannot_run_exits_with_the_shell_status(
    local_lane: LocalLane, argv: tuple[str, ...], status: int, code: str
) -> None:
    with local_lane.terminal() as terminal:
        assert local_lane.access.attach(TerminalCommand(argv, cwd=local_lane.workdir)) == status

    assert f"agw: refused: {code}".encode() in terminal.stderr


def test_a_terminal_command_whose_directory_is_missing_is_refused_before_the_terminal(local_lane: LocalLane) -> None:
    with local_lane.terminal() as terminal, pytest.raises(RequestRefused) as refused:
        local_lane.access.attach(TerminalCommand(("true",), cwd="/nonexistent/agw-5e"))

    assert refused.value.code == "cwd"
    assert terminal.calls == 0
