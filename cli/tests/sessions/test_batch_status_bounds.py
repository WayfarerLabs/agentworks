"""A tmux client that hangs, leaves, or writes without end costs only its own row in batch status.

The real batch program runs in a local shell under the request deadline that
``batch_check_status`` asks for, with a fake ``tmux`` on its PATH
and a fake ``runuser`` in place of ``tmux.RUNUSER_PATH``. The row bound is
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


def _local_guest(shell: list[str], env: dict[str, str], *, sessions: list[int] | None = None) -> RecordingVMAccess:
    """The admin's access, each request run in a local shell and, like the guest, ended at its deadline.

    Each request's shell leads a session of its own, whose id it appends to ``sessions``.
    """

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
        if sessions is not None:
            sessions.append(process.pid)
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


def _row(name: str, *, agent: bool = False, sockets: Path | None = None) -> tuple[SessionRow, SessionTmuxServer]:
    """A row and its server; ``sockets`` puts an agent's socket in that directory, named for the agent."""
    socket_path = f"{sockets or (AGENT_SOCKETS if agent else ADMIN_SOCKETS)}/{name}.sock"
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


def _live_session_members(session_id: int) -> set[int]:
    """The processes, zombies aside, still in session ``session_id``."""
    members = set()
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                # The fields after the parenthesized command name: state, ppid, pgrp, session.
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] != "Z" and int(fields[3]) == session_id:
                    members.add(int(entry.name))
    return members


def _kill_if_mentions(pid: int, marker: str) -> None:
    """SIGKILL ``pid`` if it is still a process whose command line mentions ``marker``."""
    with contextlib.suppress(OSError):
        if marker in Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"):
            os.kill(pid, signal.SIGKILL)


@requires_symlinks
@requires_posix_shell
def test_a_client_that_hangs_or_escapes_leaves_only_its_row_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: list[str]
) -> None:
    """Root's kill of each row is the one bound: no owner client holds the request open or outlives it as root's."""
    row_seconds = 2
    monkeypatch.setattr(_status, "_BATCH_ROW_SECONDS", row_seconds)
    guest_timeout = 1
    monkeypatch.setattr(_status, "GUEST_OBSERVATION_TIMEOUT_SECONDS", guest_timeout)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("base64", "cat", "head", "od", "setsid", "sleep", "stat", "timeout", "tr"):
        executable = shutil.which(name)
        assert executable is not None
        os.symlink(executable, bin_dir / name)
    owned = tmp_path / "owned"
    # One kind of client ignores SIGTERM and never answers; the other leaves the row's process group
    # and session, still holding its read's pipe, as a client its server took over could, and notes
    # its pid.
    tmux = bin_dir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  *hangs*) trap '' TERM; while :; do sleep 0.2; done ;;\n"
        '  *escapes*) printf \'%s\\n\' "$$" >> "$AGW_OWNED_PIDS"; exec "${0%/*}/setsid" "${0%/*}/sleep" 307 ;;\n'
        "esac\n"
        "exit 0\n"
    )
    tmux.chmod(0o755)
    _switch_stand_ins(bin_dir, monkeypatch)
    sockets = tmp_path / OWNER
    sockets.mkdir()
    rows = [
        _row("hangs-1"),
        _row("fine-1"),
        _row("hangs-2", agent=True),
        _row("fine-2", agent=True),
        _row("escapes", agent=True, sockets=sockets),
    ]
    requests: list[int] = []
    guest = _local_guest(
        shell,
        {"PATH": f"{bin_dir}:/bin:/usr/bin", "HOME": str(tmp_path), "AGW_OWNED_PIDS": str(owned)},
        sessions=requests,
    )

    try:
        started = time.monotonic()
        statuses = _status.batch_check_status(rows, admin=guest)
        elapsed = time.monotonic() - started
        (request,) = requests
        left = _live_session_members(request)
    finally:
        for pid in owned.read_text().split() if owned.exists() else ():
            _kill_if_mentions(int(pid), str(tmp_path))

    # However many rows hang, side by side they take one row's bound, inside the request's deadline.
    assert elapsed < guest_timeout + row_seconds
    assert statuses == {
        "hangs-1": SessionStatus.UNKNOWN,
        "fine-1": SessionStatus.RUNNING,
        "hangs-2": SessionStatus.UNKNOWN,
        "fine-2": SessionStatus.RUNNING,
        "escapes": SessionStatus.UNKNOWN,
    }
    # Every process the request's session started is gone; only the escaped client, in a session of its own, lives.
    assert left == set()


@requires_symlinks
@requires_posix_shell
def test_a_client_s_stderr_is_read_only_as_far_as_its_field_holds(
    tmp_path: Path, shell: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An owner whose client writes megabytes of diagnostic costs root one field's bytes, not the whole stream."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("base64", "cat", "head", "od", "timeout", "tr"):
        executable = shutil.which(name)
        assert executable is not None
        os.symlink(executable, bin_dir / name)
    written = tmp_path / "written"
    # The loud row's client writes 20 MB in 10 kB writes, then notes that it got them all out.
    tmux = bin_dir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        'case "$2" in *loud*)\n'
        "  i=0; while [ $i -lt 2000 ]; do printf '%010000d' 0 >&2 || exit 3; i=$((i + 1)); done\n"
        '  echo all > "$AGW_WRITTEN"; exit 1 ;;\n'
        "esac\n"
        "exit 0\n"
    )
    tmux.chmod(0o755)
    _switch_stand_ins(bin_dir, monkeypatch)
    rows = [_row("loud"), _row("fine")]
    process = subprocess.run(
        [*shell, runtime_probe_command(_status._batch_probe_command(), "BATCH")],
        input=_status._batch_probe_data(rows),
        text=True,
        capture_output=True,
        check=False,
        env={"PATH": f"{bin_dir}:/bin:/usr/bin", "HOME": str(tmp_path), "AGW_WRITTEN": str(written)},
        timeout=30,
    )

    assert not written.exists()  # the read stopped, and the writer with it
    assert len(process.stdout.splitlines()[1:]) == 2  # every row reported, the loud one included
    guest = RecordingVMAccess([], Admin())
    guest.default_outcome = Result(Completed(process.returncode), process.stdout.encode(), process.stderr.encode())
    assert _status.batch_check_status(rows, admin=guest) == {
        "loud": SessionStatus.UNKNOWN,
        "fine": SessionStatus.RUNNING,
    }


@requires_symlinks
@requires_posix_shell
def test_a_row_line_stays_under_pipe_buf_whatever_its_clients_print(
    tmp_path: Path, shell: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("base64", "cat", "head", "od", "timeout", "tr"):
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


@requires_symlinks
@requires_posix_shell
def test_a_row_whose_socket_fails_the_guard_is_unknown_and_never_reaches_tmux(
    tmp_path: Path, shell: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch program refuses a row on the condition every other owner read tests (``socket_guard_condition``)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("base64", "cat", "head", "od", "stat", "timeout", "tr"):
        executable = shutil.which(name)
        assert executable is not None
        os.symlink(executable, bin_dir / name)
    audit = tmp_path / "audit"
    tmux = bin_dir / "tmux"
    tmux.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$AGW_PROBE_AUDIT"\nexit 0\n')
    tmux.chmod(0o755)
    _switch_stand_ins(bin_dir, monkeypatch)
    sockets = tmp_path / OWNER
    sockets.mkdir()
    linked = sockets / "linked.sock"
    linked.symlink_to(sockets / "elsewhere.sock")
    plain = sockets / "plain.sock"
    plain.write_text("")
    rows = [_row(name, agent=True, sockets=sockets) for name in ("linked", "plain", "fine")]
    guest = _local_guest(
        shell, {"PATH": f"{bin_dir}:/bin:/usr/bin", "HOME": str(tmp_path), "AGW_PROBE_AUDIT": str(audit)}
    )

    assert _status.batch_check_status(rows, admin=guest) == {
        "linked": SessionStatus.UNKNOWN,
        "plain": SessionStatus.UNKNOWN,
        "fine": SessionStatus.RUNNING,
    }
    assert [line for line in audit.read_text().splitlines() if "linked" in line or "plain" in line] == []
