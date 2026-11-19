"""Every tmux client switched to a server's owner runs as that owner, from ``/``.

Each builder's command runs in a real shell, started in a scratch directory,
with a fake ``sudo`` that records the directory it was started in and its
arguments. A switched client must never start in the caller's directory.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.db import SessionRow
from agentworks.sessions import tmux
from agentworks.sessions.manager import SessionTmuxServer, _status
from tests.conftest import requires_posix_shell

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

OWNER = "agt-owner"
SOCKET = f"/run/agentworks/agent-tmux-sockets/{OWNER}/s1.sock"
SWITCH = ["-n", tmux.RUNUSER_PATH, "-u", OWNER, "--"]


class _Capture:
    """Transport double that keeps the command a ``RunCommand`` hands it."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: Any) -> None:
        self.commands.append(command)


def _run_command_program() -> str:
    target = _Capture()
    tmux.server_owner_run_command(target, OWNER)("tmux -S x list-sessions; printf done", check=False)  # type: ignore[arg-type]
    return target.commands[0]


def _batch_program() -> tuple[str, str]:
    session = SessionRow(name="s1", workspace_name="ws", template="default", mode="agent", created_at="", updated_at="")
    rows = [(session, SessionTmuxServer(SOCKET, OWNER))]
    return _status._batch_probe_command(), _status._batch_probe_data(rows)


CASES: dict[str, tuple[Callable[[], str | tuple[str, str]], list[list[str]]]] = {
    "probe (named console window)": (
        lambda: tmux.tmux_cmd("has-session -t '=s1'", SOCKET, owner=OWNER),
        [[*SWITCH, "tmux", "-S", SOCKET, "has-session", "-t", "=s1"]],
    ),
    "interactive attach": (
        lambda: tmux.tmux_attach_cmd("s1", SOCKET, owner=OWNER),
        [["-n", tmux.RUNUSER_PATH, "--pty", "-u", OWNER, "--", "tmux", "-S", SOCKET, "attach", "-t", "=s1"]],
    ),
    "transport run command": (
        _run_command_program,
        [[*SWITCH, "/bin/bash", "-c", "tmux -S x list-sessions; printf done"]],
    ),
    "batch status": (
        _batch_program,
        [
            [*SWITCH, "timeout", "-k", "1", "2", "tmux", "-S", SOCKET, "has-session", "-t", "=s1"],
            [*SWITCH, "timeout", "-k", "1", "2", "tmux", "-S", SOCKET, "list-sessions"],
        ],
    ),
}


@requires_posix_shell
@pytest.mark.parametrize("case", CASES)
def test_switched_clients_run_as_the_owner_from_the_root_directory(tmp_path: Path, case: str) -> None:
    build, expected = CASES[case]
    program = build()
    program, stdin = program if isinstance(program, tuple) else (program, "")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    audit = tmp_path / "audit"
    sudo = bin_dir / "sudo"
    # One line per call: the starting directory, then each argument, unit-separated.
    sudo.write_text(
        "#!/bin/sh\n"
        'printf %s "$(pwd -P)" >> "$AGW_SUDO_AUDIT"\n'
        'for arg in "$@"; do printf \'\\037%s\' "$arg" >> "$AGW_SUDO_AUDIT"; done\n'
        "printf '\\n' >> \"$AGW_SUDO_AUDIT\"\n"
    )
    sudo.chmod(0o755)
    caller = tmp_path / "caller"
    caller.mkdir()

    subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", program],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        cwd=caller,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "AGW_SUDO_AUDIT": str(audit)},
        timeout=10,
    )

    switches = [line.split("\x1f") for line in audit.read_text().splitlines()]
    assert [switch[0] for switch in switches] == ["/"] * len(expected)
    assert [switch[1:] for switch in switches] == expected


def test_a_server_the_caller_owns_runs_its_command_unchanged() -> None:
    target = _Capture()
    tmux.server_owner_run_command(target, None)("tmux -S x list-sessions", check=False)  # type: ignore[arg-type]
    assert target.commands == ["tmux -S x list-sessions"]
    assert tmux.tmux_attach_cmd("s1", SOCKET, owner=None) == tmux.tmux_cmd("attach -t '=s1'", SOCKET)
