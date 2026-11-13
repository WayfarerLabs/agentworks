"""What ``attach`` does around the terminal half: the guard, and the notice that must follow it."""

from __future__ import annotations

import contextlib
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from agentworks import output
from agentworks.execution import attach as attach_module
from agentworks.execution import ssh_process
from agentworks.execution.requests import LoginShell
from tests.conftest import pretend_windows_console

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from tests.execution.conformance.lanes import LocalLane


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="agw-launch needs Linux")


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    log: list[tuple[str, Any]] = []

    @contextlib.contextmanager
    def guard() -> Iterator[None]:
        log.append(("enter", None))
        try:
            yield
        finally:
            log.append(("exit", None))

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


def test_the_terminal_is_restored_when_the_terminal_half_is_interrupted(
    local_lane: LocalLane, events: list[tuple[str, Any]]
) -> None:
    def interrupted(argv: list[str], half: object) -> int:
        raise KeyboardInterrupt

    token = local_lane.handover_token()
    try:
        with local_lane.terminal(behavior=interrupted), pytest.raises(KeyboardInterrupt):
            local_lane.access.attach(LoginShell(cwd=local_lane.workdir))

        assert events == [("enter", None), ("exit", None)]  # I21
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


@pytest.mark.parametrize(("platform", "first"), [("win32", b"\0hi\n"), ("linux", b"hi\n")])
def test_on_a_windows_console_the_terminal_half_writes_a_nul_first_and_keeps_the_shells_exit_status(
    local_lane: LocalLane,
    events: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    first: bytes,
) -> None:
    pretend_windows_console(monkeypatch)
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr(ssh_process, "_CREATE_NO_WINDOW", 0)  # this host cannot create a console-free process
    with local_lane.terminal(b"echo hi\nexit 7\n") as terminal:
        code = local_lane.access.attach(LoginShell(cwd=local_lane.workdir))

    assert code == 7
    assert terminal.stdout.startswith(first)


def test_only_the_parking_connection_leaves_the_operators_console(
    local_lane: LocalLane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #889: the parked client must not share the console the terminal half runs on."""
    spawned: list[bool] = []
    real_spawn = ssh_process.spawn

    def spawn(argv: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        spawned.append(kwargs.get("share_console", True))
        return real_spawn(argv, **kwargs)

    monkeypatch.setattr(ssh_process, "spawn", spawn)
    with local_lane.terminal(behavior=lambda argv, half: 0):
        local_lane.access.attach(LoginShell(cwd=local_lane.workdir))

    assert spawned == [False]  # the terminal half is stubbed here; it spawns with the default
