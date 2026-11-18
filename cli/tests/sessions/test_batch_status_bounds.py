"""Stuck tmux servers leave only their own rows unknown in batch status.

The real batch program runs in a local shell under the request deadline that
``batch_check_status`` asks for, with a fake ``tmux`` on its PATH
and a fake ``runuser`` in place of ``tmux.RUNUSER_PATH``. The timeouts are
shortened so the test stays quick.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from agentworks.db import SessionMode, SessionRow, SessionStatus
from agentworks.execution.requests import Bytes, Script
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.sessions import tmux as tmux_module
from agentworks.sessions.manager import SessionTmuxServer, _status
from agentworks.sessions.tmux import runtime_probe_command
from tests.conftest import requires_posix_shell, requires_symlinks
from tests.vm_access_support import Admin, Call, RecordingVMAccess

BOOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ADMIN = "admin"
ADMIN_SOCKETS = f"/run/agentworks/admin-tmux-sockets/{ADMIN}"
OWNER = "agt-owner"
AGENT_SOCKETS = f"/run/agentworks/agent-tmux-sockets/{OWNER}"


# The admin's login shell runs the batch program; each runs without its startup files.
_SHELLS = {
    "sh": ("-c",),
    "dash": ("-c",),
    "bash": ("--noprofile", "--norc", "-c"),
    "zsh": ("-f", "-c"),
}


@pytest.fixture(params=sorted(_SHELLS))
def shell(request: pytest.FixtureRequest) -> list[str]:
    """The argv prefix that runs a program in one of the shells the admin may log in with."""
    path = shutil.which(request.param)
    if path is None:
        pytest.skip(f"{request.param} is not installed")
    return [path, *_SHELLS[request.param]]


def _local_guest(shell: list[str], env: dict[str, str]) -> RecordingVMAccess:
    """The admin's access, each request run in a local shell and, like the guest, ended at its deadline."""

    def run(call: Call) -> Result:
        request = call.request
        assert isinstance(request, Script) and isinstance(request.stdin, Bytes) and request.deadline is not None
        process = subprocess.Popen(
            [*shell, request.source],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(request.stdin.data, timeout=request.deadline)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return Result(Unknown(UnknownReason.DEADLINE))
        return Result(Completed(process.returncode), stdout, stderr)

    access = RecordingVMAccess([], Admin())
    access.answer = run
    return access


def _switch_stand_ins(bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Root's switch to each row's account, reduced to running the client as the caller."""
    runuser = bin_dir / "runuser"
    runuser.write_text('#!/bin/sh\n[ "$1" = -u ] && [ "$3" = -- ] || exit 97\nshift 3\nexec "$@"\n')
    runuser.chmod(0o755)
    monkeypatch.setattr(tmux_module, "RUNUSER_PATH", str(runuser))


def _row(name: str, *, agent: bool = False) -> tuple[SessionRow, SessionTmuxServer]:
    socket_path = f"{AGENT_SOCKETS if agent else ADMIN_SOCKETS}/{name}.sock"
    session = SessionRow(
        name=name,
        workspace_name="ws",
        template="default",
        mode=(SessionMode.AGENT if agent else SessionMode.ADMIN).value,
        created_at="",
        updated_at="",
        pid=os.getpid(),
        socket_path=socket_path,
        boot_id=BOOT,
    )
    return session, SessionTmuxServer(socket_path, OWNER if agent else None)


def _processes_mentioning(marker: str) -> list[str]:
    """Command lines of live processes that mention ``marker``."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                if marker in command:
                    found.append(command)
    return found


@requires_symlinks
@requires_posix_shell
def test_stuck_servers_leave_only_their_own_rows_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: list[str]
) -> None:
    # A row's worst case is then 2 * (1 + 1) = 4 seconds, and one row's bound on top of the guest
    # timeout is 1 + 4. Each stuck client ignores SIGTERM and so lasts until timeout's SIGKILL, 2
    # seconds: three stuck rows in turn would need 6, so only rows probed side by side fit.
    monkeypatch.setattr(_status, "_BATCH_CLIENT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(_status, "_BATCH_CLIENT_KILL_SECONDS", 1)
    guest_timeout = 1
    monkeypatch.setattr(_status, "GUEST_OBSERVATION_TIMEOUT_SECONDS", guest_timeout)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("base64", "cat", "od", "sleep", "timeout", "tr"):
        executable = shutil.which(name)
        assert executable is not None
        os.symlink(executable, bin_dir / name)
    audit = tmp_path / "audit"
    tmux = bin_dir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$AGW_PROBE_AUDIT"\n'
        "case \"$2\" in *stuck*) trap '' TERM; while :; do sleep 0.2; done ;; esac\n"
        "exit 0\n"
    )
    tmux.chmod(0o755)
    _switch_stand_ins(bin_dir, monkeypatch)
    rows = [
        _row("stuck-1"),
        _row("fine-1"),
        _row("stuck-2", agent=True),
        _row("fine-2", agent=True),
        _row("stuck-3"),
    ]
    guest = _local_guest(
        shell, {"PATH": f"{bin_dir}:/bin:/usr/bin", "HOME": str(tmp_path), "AGW_PROBE_AUDIT": str(audit)}
    )

    started = time.monotonic()
    statuses = _status.batch_check_status(rows, admin=guest)
    elapsed = time.monotonic() - started

    # However many servers are stuck, the batch takes about one row's worst case.
    assert elapsed < guest_timeout + _status._batch_row_bound_seconds()

    assert statuses == {
        "stuck-1": SessionStatus.UNKNOWN,
        "fine-1": SessionStatus.RUNNING,
        "stuck-2": SessionStatus.UNKNOWN,
        "fine-2": SessionStatus.RUNNING,
        "stuck-3": SessionStatus.UNKNOWN,
    }
    # A server that did not answer has-session gets no second client.
    assert [line for line in audit.read_text().splitlines() if "stuck" in line and "list-sessions" in line] == []
    # Every stuck client was killed, and no row's job outlived the request.
    assert _processes_mentioning(str(bin_dir)) == []


@requires_symlinks
@requires_posix_shell
def test_a_row_line_stays_under_pipe_buf_whatever_its_clients_print(
    tmp_path: Path, shell: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("base64", "cat", "od", "timeout", "tr"):
        executable = shutil.which(name)
        assert executable is not None
        os.symlink(executable, bin_dir / name)
    # The loud row's clients fail with a repetitive diagnostic far longer than a pipe's atomic write.
    tmux = bin_dir / "tmux"
    tmux.write_text("#!/bin/sh\ncase \"$2\" in *loud*) printf '%05000d' 0 >&2; exit 1 ;; esac\nexit 0\n")
    tmux.chmod(0o755)
    _switch_stand_ins(bin_dir, monkeypatch)
    rows = [_row("loud"), _row("fine")]
    process = subprocess.run(
        [*shell, runtime_probe_command(_status._batch_probe_command(), "BATCH")],
        input=_status._batch_probe_data(rows),
        text=True,
        capture_output=True,
        check=False,
        env={"PATH": f"{bin_dir}:/bin:/usr/bin", "HOME": str(tmp_path)},
        timeout=10,
    )

    lines = process.stdout.splitlines()[1:]  # after the frame marker
    assert len(lines) == 2 and all(len(line.encode()) < os.fpathconf(0, "PC_PIPE_BUF") for line in lines)
    guest = RecordingVMAccess([], Admin())
    guest.default_outcome = Result(Completed(process.returncode), process.stdout.encode(), process.stderr.encode())
    # The capped diagnostics are no tmux fact, so that row is unknown, not absent, and its line
    # stays well formed, so its sibling is still classified.
    assert _status.batch_check_status(rows, admin=guest) == {
        "loud": SessionStatus.UNKNOWN,
        "fine": SessionStatus.RUNNING,
    }
