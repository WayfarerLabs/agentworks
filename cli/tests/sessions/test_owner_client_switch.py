"""Every tmux client switched to a server's owner runs as that owner, from ``/``, switched by root.

Each builder's command runs in a real shell, started in a scratch directory,
with a fake ``sudo`` and a fake ``runuser`` in place of ``tmux.RUNUSER_PATH``
that record, in order, the directory they were started in and their
arguments. A caller on the admin's own shell elevates its whole line with
``sudo -n /bin/sh -c`` so root runs the socket guard; a request the admin's
access sends elevated is already root. Either way root then runs ``runuser``
itself: no switch goes through a second ``sudo``, and a switched client
never starts in the caller's directory.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import TYPE_CHECKING

import pytest

from agentworks.db import SessionMode, SessionRow
from agentworks.execution.requests import Bytes, Script
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
# The admin's elevation of a whole line, so root runs the guard and the switch.
ADMIN_ELEVATES = ["sudo", "-n", "/bin/sh", "-c"]
SWITCH = ["runuser", "-u", OWNER, "--"]


def _session() -> SessionRow:
    return SessionRow(
        name="s1",
        workspace_name="ws",
        template="default",
        mode=SessionMode.AGENT.value,
        created_at="",
        updated_at="",
        socket_path=SOCKET,
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
    access = RecordingVMAccess([], Admin())
    _status.batch_check_status([(_session(), SessionTmuxServer(SOCKET, OWNER))], admin=access)
    (request,) = access.requests()
    assert isinstance(request, Script) and request.elevated and isinstance(request.stdin, Bytes)
    return request.source, request.stdin.data.decode()


CASES: dict[str, tuple[Callable[[], str | tuple[str, str]], list[list[str]]]] = {
    "probe (named console window)": (
        lambda: tmux.as_socket_owner_from_admin(
            tmux.tmux_cmd("has-session -t '=s1'", SOCKET), socket_path=SOCKET, owner=OWNER
        ),
        [ADMIN_ELEVATES, [*SWITCH, "/bin/sh", "-c", f"tmux -S {SOCKET} has-session -t '=s1'"]],
    ),
    "interactive attach": (
        _attach_program,
        [
            ADMIN_ELEVATES,
            ["runuser", "--pty", "-u", OWNER, "--", "/bin/sh", "-c", f"tmux -S {SOCKET} attach -t '=s1'"],
        ],
    ),
    "access run command (elevated request)": (
        _run_command_program,
        [[*SWITCH, "/bin/sh", "-c", "tmux -S x list-sessions; printf done"]],
    ),
    "batch status (elevated request)": (
        _batch_program,
        [
            [*SWITCH, "timeout", "-k", "1", "2", "tmux", "-S", SOCKET, "has-session", "-t", "=s1"],
            [*SWITCH, "timeout", "-k", "1", "2", "tmux", "-S", SOCKET, "list-sessions"],
        ],
    ),
}


def _recorder(path: Path, name: str, *, then: str) -> None:
    """A stand-in that records one line, then runs ``then``.

    The line is its name, its starting directory and each argument, unit-separated.
    """
    path.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\037%s" {name} "$(pwd -P)" >> "$AGW_SWITCH_AUDIT"\n'
        'for arg in "$@"; do printf \'\\037%s\' "$arg" >> "$AGW_SWITCH_AUDIT"; done\n'
        "printf '\\n' >> \"$AGW_SWITCH_AUDIT\"\n"
        f"{then}\n"
    )
    path.chmod(0o755)


@requires_posix_shell
@pytest.mark.parametrize("case", CASES)
def test_switched_clients_run_as_the_owner_from_the_root_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    build, expected = CASES[case]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    audit = tmp_path / "audit"
    # The admin's elevation runs its line; anything else through sudo is recorded and fails.
    _recorder(bin_dir / "sudo", "sudo", then='[ "$1 $2 $3" = "-n /bin/sh -c" ] || exit 97; shift; exec "$@"')
    runuser = tmp_path / "runuser"
    _recorder(runuser, "runuser", then="exit 0")
    monkeypatch.setattr(tmux, "RUNUSER_PATH", str(runuser))
    program = build()
    program, stdin = program if isinstance(program, tuple) else (program, "")
    caller = tmp_path / "caller"
    caller.mkdir()

    subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", program],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        cwd=caller,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "AGW_SWITCH_AUDIT": str(audit)},
        timeout=10,
    )

    records = [line.split("\x1f") for line in audit.read_text().splitlines()]
    # The elevation's own argument, the whole guarded line, is left out.
    calls = [[name, *(args[:3] if name == "sudo" else args)] for name, _directory, *args in records]
    assert calls == expected
    assert [directory for name, directory, *_args in records if name == "runuser"] == ["/"] * (
        len(expected) - expected.count(ADMIN_ELEVATES)
    )


def test_a_server_the_caller_owns_runs_its_command_without_a_switch() -> None:
    source = _run_command_program(switch_user=False)

    assert owner_switch(source) is None
    assert source.endswith("; tmux -S x list-sessions; printf done")
