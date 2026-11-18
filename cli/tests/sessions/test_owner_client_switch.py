"""Every tmux client switched to a server's owner runs as that owner, from ``/``.

Each builder's command runs in a real shell, started in a scratch directory,
with a fake ``sudo`` that records the directory it was started in and its
arguments. A ``sudo -n /bin/sh -c`` call, the admin's own elevation to run
the socket guard as root, runs its line unrecorded, so only the switch to the
owner is recorded. A switched client must never start in the caller's
directory.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import TYPE_CHECKING

import pytest

from agentworks.db import SessionMode, SessionRow
from agentworks.execution.requests import Script
from agentworks.sessions import tmux
from agentworks.sessions.manager import SessionTmuxServer, _status
from agentworks.sessions.manager._queries import _attach_command
from tests._runtime_probes import owner_switch
from tests.conftest import requires_posix_shell
from tests.vm_access_support import Admin, RecordingVMAccess

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

OWNER = "agt-owner"
SOCKET = f"/run/agentworks/agent-tmux-sockets/{OWNER}/s1.sock"
SWITCH = ["-n", tmux.RUNUSER_PATH, "-u", OWNER, "--"]


def _session() -> SessionRow:
    return SessionRow(
        name="s1", workspace_name="ws", template="default", mode=SessionMode.AGENT.value, created_at="", updated_at=""
    )


def _run_command_program(*, switch_user: bool = True) -> str:
    access = RecordingVMAccess([], Admin())
    tmux.socket_owner_run_command(access, socket_path=SOCKET, owner=OWNER, switch_user=switch_user, deadline=None)(
        "tmux -S x list-sessions; printf done", check=False
    )
    (request,) = access.requests()
    assert isinstance(request, Script) and request.elevated is switch_user
    return request.source


def _attach_program() -> str:
    return shlex.join(_attach_command(_session(), SessionTmuxServer(SOCKET, OWNER)).argv)


def _batch_program() -> tuple[str, str]:
    rows = [(_session(), SessionTmuxServer(SOCKET, OWNER))]
    return _status._batch_probe_command(), _status._batch_probe_data(rows)


CASES: dict[str, tuple[Callable[[], str | tuple[str, str]], list[list[str]]]] = {
    "probe (named console window)": (
        lambda: tmux.as_socket_owner_from_admin(
            tmux.tmux_cmd("has-session -t '=s1'", SOCKET), socket_path=SOCKET, owner=OWNER
        ),
        [[*SWITCH, "/bin/sh", "-c", f"tmux -S {SOCKET} has-session -t '=s1'"]],
    ),
    "interactive attach": (
        _attach_program,
        [["-n", tmux.RUNUSER_PATH, "--pty", "-u", OWNER, "--", "/bin/sh", "-c", f"tmux -S {SOCKET} attach -t '=s1'"]],
    ),
    "access run command": (
        _run_command_program,
        [[*SWITCH, "/bin/sh", "-c", "tmux -S x list-sessions; printf done"]],
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
    # One line per switch: the starting directory, then each argument, unit-separated.
    sudo.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = /bin/sh ]; then shift; exec "$@"; fi\n'
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


def test_a_server_the_caller_owns_runs_its_command_without_a_switch() -> None:
    source = _run_command_program(switch_user=False)

    assert owner_switch(source) is None
    assert source.endswith("; tmux -S x list-sessions; printf done")
