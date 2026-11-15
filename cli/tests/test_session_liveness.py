"""Tests for session liveness checking functions."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import PID_STOPPED, SessionMode, SessionRow, SessionRuntimeSnapshot, SessionStatus
from agentworks.errors import ConnectivityError, StateError
from agentworks.execution.errors import ExecutionIncomplete, TrustMismatch, TrustMissing
from agentworks.execution.requests import Bytes, Script
from agentworks.execution.results import Completed, Result, Unknown, UnknownReason
from agentworks.sessions.manager import (
    _needs_repair,
    _repair_session_pid,
    _validated_stored_start_ticks,
    batch_check_status,
    check_session_status,
    observe_session_statuses,
)
from agentworks.sessions.manager._status import (
    _batch_probe_command,
    _batch_probe_data,
    _encoded_probe_field,
)
from agentworks.sessions.tmux import (
    ADMIN_SOCKET_ROOT,
    AGENT_SOCKET_ROOT,
    ProbeStatus,
    probe_tmux_server_pid,
    runtime_probe_command,
)
from agentworks.status_observation import GUEST_OBSERVATION_TIMEOUT_SECONDS
from tests._runtime_probes import framed_runtime_result
from tests.conftest import requires_posix_shell, requires_symlinks
from tests.vm_access_support import Admin, Call, RecordingVMAccess, answer_from_target

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


ADMIN_SOCKETS = f"{ADMIN_SOCKET_ROOT}/agentworks"
"""The socket directory of the test database's VM admin, ``insert_vm``'s default ``agentworks``."""


@dataclass
class _FakeResult:
    stdout: str = ""
    stderr: str = ""
    ok: bool = True
    returncode: int = -1  # auto-set from ok in __post_init__

    def __post_init__(self) -> None:
        if self.returncode == -1:
            self.returncode = 0 if self.ok else 1


class _FakeTarget:
    """A guest that returns canned responses keyed by substring match; :func:`_admin` puts an access over it."""

    def __init__(self, responses: dict[str, _FakeResult] | None = None) -> None:
        self._responses = responses or {}
        self.commands: list[str] = []
        self.call_options: list[tuple[bool, float | None]] = []

    def run(
        self,
        command: str,
        *,
        check: bool = True,
        sudo: bool = False,
        timeout: float | None = None,
        input_data: str | None = None,
    ) -> _FakeResult:
        self.commands.append(command)
        self.call_options.append((sudo, timeout))
        for pattern, result in self._responses.items():
            if pattern in command:
                return framed_runtime_result(command, result)
        return framed_runtime_result(command, _FakeResult())


def _admin(target: object) -> RecordingVMAccess:
    """The admin's access, each request answered by ``target`` as its command line, ``sudo`` for root's."""
    access = RecordingVMAccess([], Admin())
    access.answer = answer_from_target(target)
    return access


def _session(
    name: str,
    pid: int | None = None,
    socket_path: str | None = None,
    mode: str = "admin",
    boot_id: str | None = None,
) -> SessionRow:
    return SessionRow(
        name=name,
        workspace_name="ws",
        template="default",
        mode=mode,
        created_at="",
        updated_at="",
        pid=pid,
        socket_path=socket_path,
        boot_id=boot_id,
    )


def _owner(session: SessionRow) -> str:
    """The socket owner the controller names for ``session``: the admin, or an agent account of its own."""
    return "admin" if session.mode == "admin" else f"agt-{session.name}"


def _batch(sessions: list[SessionRow], *, admin: RecordingVMAccess) -> dict[str, SessionStatus]:
    """``batch_check_status`` with each row's owner as the controller names it."""
    return batch_check_status(sessions, admin=admin, owners={session.name: _owner(session) for session in sessions})


def _stdin_rows(request: Script) -> list[tuple[str, str, str, str]]:
    """The batch request's stdin rows, decoded: name, socket path, owner and pid field."""
    import base64

    assert isinstance(request.stdin, Bytes)
    rows = []
    for line in request.stdin.data.decode().splitlines():
        name, socket_path, owner, pid = line.split(" ")
        rows.append(
            (
                base64.b64decode(name).decode(),
                base64.b64decode(socket_path).decode(),
                base64.b64decode(owner).decode(),
                pid,
            )
        )
    return rows


class _Owners:
    """The database reads ``_validated_socket`` makes: every agent's user is ``agt``, every VM's admin ``admin``."""

    def get_agent(self, _name: str) -> SimpleNamespace:
        return SimpleNamespace(linux_user="agt")

    def get_workspace(self, _name: str) -> SimpleNamespace:
        return SimpleNamespace(vm_name="box")

    def get_vm(self, _name: str) -> SimpleNamespace:
        return SimpleNamespace(admin_username="admin")


def _owned(session: SessionRow) -> SessionRow:
    """``session`` with a dedicated socket moved into its owner's managed directory, as ``_Owners`` names it."""
    from dataclasses import replace
    from pathlib import PurePosixPath

    if session.socket_path is None:
        return session
    root, owner = (AGENT_SOCKET_ROOT, "agt") if session.mode == "agent" else (ADMIN_SOCKET_ROOT, "admin")
    return replace(session, socket_path=f"{root}/{owner}/{PurePosixPath(session.socket_path).stem}.sock")


def _check(session: SessionRow, *, admin: RecordingVMAccess) -> SessionStatus:
    """``check_session_status`` of ``session``, its socket in its owner's managed directory."""
    return check_session_status(_owned(session), admin=admin, db=_Owners())  # type: ignore[arg-type]


def _batch_row(
    name: str,
    *,
    has_returncode: int = 0,
    has_diagnostic: str = "",
    server_returncode: int = 0,
    server_diagnostic: str = "",
    boot_returncode: int = 0,
    boot_id: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    pid_returncode: int = 0,
    pid_fact: str = "present",
) -> str:
    return (
        f"S:{_encoded_probe_field(name)}:{has_returncode}:{has_diagnostic.encode().hex()}:"
        f"{server_returncode}:{server_diagnostic.encode().hex()}:"
        f"{boot_returncode}:{boot_id.encode().hex()}:{pid_returncode}:{pid_fact.encode().hex()}"
    )


# -- batch_check_status -----------------------------------------------------


def test_batch_mixed() -> None:
    """Batch: agent running, admin stopped, NULL-PID excluded."""
    sessions = [
        _session("a1", pid=100, socket_path="/sock1", mode="agent", boot_id=BOOT_CURRENT),
        _session("s1", pid=200, socket_path="/sock2", mode="admin", boot_id=BOOT_CURRENT),
        _session("s2", pid=None),
    ]
    target = _FakeTarget(
        {
            "has-session": _FakeResult(
                ok=True,
                stdout=(
                    _batch_row("a1")
                    + "\n"
                    + _batch_row(
                        "s1",
                        has_returncode=1,
                        has_diagnostic="can't find session: s1",
                        server_returncode=1,
                        server_diagnostic="no server running on /sock2",
                        boot_id=BOOT_STALE,
                        pid_returncode=1,
                        pid_fact="",
                    )
                    + "\n"
                ),
            ),
        }
    )
    result = _batch(sessions, admin=_admin(target))
    assert result["a1"] == SessionStatus.RUNNING
    assert result["s1"] == SessionStatus.STOPPED
    assert "s2" not in result


def test_batch_empty() -> None:
    assert _batch([], admin=_admin(_FakeTarget())) == {}


def test_batch_all_missing_pid() -> None:
    sessions = [_session("s1", pid=None)]
    assert _batch(sessions, admin=_admin(_FakeTarget())) == {}


def test_session_observer_treats_disappeared_workspace_as_unknown() -> None:
    class _DB:
        def get_workspace(self, _name: str) -> None:
            return None

    statuses = observe_session_statuses(
        [_session("s1", socket_path="/sock")],
        db=_DB(),
        config=object(),  # type: ignore[arg-type]
    )
    assert statuses == {"s1": SessionStatus.UNKNOWN}


def test_session_observer_preserves_corrupt_ssh_applied_state(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("box", site="site", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
    db.insert_session("alpha", "ws", "default", SessionMode.ADMIN, socket_path=f"{ADMIN_SOCKETS}/alpha.sock")
    session = db.get_session("alpha")
    assert session is not None
    structural = StateError("corrupt SSH applied state", entity_kind="vm", entity_name="box")
    monkeypatch.setattr(
        "agentworks.vms.manager.require_vm_ssh_boundary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(structural),
    )
    monkeypatch.setattr(
        "agentworks.sessions.manager._status.batch_check_status",
        lambda *_args, **_kwargs: pytest.fail("observed after structural failure"),
    )

    with pytest.raises(StateError) as caught:
        observe_session_statuses([session], db=db, config=object())

    assert caught.value is structural


def test_batch_status_is_one_elevated_request_within_the_deadline_with_each_row_and_its_owner_on_stdin() -> None:
    sessions = [
        _session("a1", pid=100, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT),
        _session("s1", pid=None, socket_path="/sock2", mode="admin", boot_id=BOOT_CURRENT),
    ]
    target = _FakeTarget({"has-session": _FakeResult(ok=True, stdout=f"{_batch_row('a1')}\n{_batch_row('s1')}\n")})
    admin = _admin(target)

    _batch(sessions, admin=admin)

    (request,) = admin.requests()
    assert isinstance(request, Script)
    assert (request.elevated, request.deadline) == (True, GUEST_OBSERVATION_TIMEOUT_SECONDS)
    assert _stdin_rows(request) == [("a1", "/sock", "agt-a1", "100"), ("s1", "/sock2", "admin", "-")]


@pytest.mark.parametrize("mode", ["admin", "agent"])
def test_singular_status_reads_each_run_once_within_the_deadline_as_root_only_for_an_agent(mode: str) -> None:
    session = _session("s1", pid=42, socket_path="/sock", mode=mode, boot_id=BOOT_CURRENT)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
            "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
            "test -d /proc/42": _FakeResult(ok=True, stdout="absent"),
        }
    )

    assert _check(session, admin=_admin(target)) is SessionStatus.STOPPED
    assert len(target.commands) == 4
    assert target.call_options == [(mode == "agent", GUEST_OBSERVATION_TIMEOUT_SECONDS)] * 4


def test_a_status_read_that_does_not_complete_raises_after_one_dispatch_and_the_listing_reads_unknown(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I88: a VM that does not answer within the deadline costs one dispatch and reads as unknown."""
    from tests.vm_access_support import install_access_recorder

    db.insert_vm("box", site="site", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
    db.insert_session("alpha", "ws", "default", SessionMode.ADMIN, socket_path=f"{ADMIN_SOCKETS}/alpha.sock")
    session = db.get_session("alpha")
    assert session is not None
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *_args, **_kwargs: None)
    recorder = install_access_recorder(monkeypatch)
    recorder.answer = lambda call: Result(Unknown(UnknownReason.DEADLINE))

    with pytest.raises(ExecutionIncomplete):
        check_session_status(session, admin=recorder.access(), db=db)
    assert observe_session_statuses([session], db=db, config=object()) == {"alpha": SessionStatus.UNKNOWN}

    assert [request.deadline for request in recorder.requests("run")] == [GUEST_OBSERVATION_TIMEOUT_SECONDS] * 2


def test_batch_fleet_sends_a_constant_script_and_every_row_on_its_input() -> None:
    from agentworks.sessions.tmux import MAX_SESSION_NAME_LENGTH, SUN_PATH_MAX

    sessions = []
    for index in range(512):
        suffix = f"{index:08x}"
        name = ("s" * (MAX_SESSION_NAME_LENGTH - len(suffix))) + suffix
        fixed = f"/{suffix}/{name}"
        socket_path = "/" + ("p" * (SUN_PATH_MAX - 2 - len(fixed))) + fixed
        assert len(socket_path) == SUN_PATH_MAX - 1
        sessions.append(_session(name, pid=100_000 + index, socket_path=socket_path, mode="agent"))

    admin = RecordingVMAccess([], Admin())
    admin.default_outcome = Result(
        Completed(0),
        ("\nAGW_RUNTIME_BATCH=" + "\n".join(_batch_row(session.name) for session in sessions) + "\n").encode(),
    )

    status_map = _batch(sessions, admin=admin)

    (request,) = admin.requests()
    assert isinstance(request, Script)
    assert request.source == runtime_probe_command(_batch_probe_command(), "BATCH")
    assert _stdin_rows(request) == [
        (session.name, session.socket_path, _owner(session), str(session.pid)) for session in sessions
    ]
    assert isinstance(request.stdin, Bytes) and len(request.stdin.data) > 32_767
    assert status_map == {session.name: SessionStatus.RUNNING for session in sessions}


@requires_posix_shell
def test_batch_command_is_valid_shell_syntax() -> None:
    # Skips on Windows: this checks the guest-target batch probe with ``bash -n``,
    # which Git-for-Windows' bash parses differently. Linux CI covers it.
    command = _batch_probe_command()

    result = subprocess.run(["bash", "-n", "-c", command], check=False, capture_output=True)

    assert result.returncode == 0


@requires_symlinks
@requires_posix_shell
def test_batch_probe_reads_each_row_as_its_owner_and_refuses_a_socket_it_cannot_trust(tmp_path: Path) -> None:
    """Only read-only guest operations run, each row's tmux reads run as its owner through ``runuser``, and a
    socket that is a link, is not a socket, or belongs to another account is never handed to tmux."""
    import pwd
    import socket
    import tempfile
    from pathlib import Path

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    audit = tmp_path / "audit"

    for name in ("base64", "cat", "od", "stat", "tr"):
        executable = shutil.which(name)
        assert executable is not None
        os.symlink(executable, bin_dir / name)

    tmux = bin_dir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        'printf tmux >> "$AGW_PROBE_AUDIT"\n'
        'for arg in "$@"; do printf \'\\t%s\' "$arg" >> "$AGW_PROBE_AUDIT"; done\n'
        "printf '\\n' >> \"$AGW_PROBE_AUDIT\"\n"
    )
    tmux.chmod(0o755)

    # Root drops to each row's owner; this stand-in records the account and runs the rest as the caller.
    runuser = bin_dir / "runuser"
    runuser.write_text(
        '#!/bin/sh\n[ "$1" = -u ] && [ "$3" = -- ] || exit 64\n'
        'printf \'runuser\\t%s\\n\' "$2" >> "$AGW_PROBE_AUDIT"\nshift 3\nexec "$@"\n'
    )
    runuser.chmod(0o755)

    # The script runs elevated as a whole, so it never elevates a row itself.
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\nprintf \'unexpected\\tsudo\\t%s\\n\' "$*" >> "$AGW_PROBE_AUDIT"\nexit 97\n')
    sudo.chmod(0o755)

    shell = bin_dir / "sh"
    shell.write_text(
        "#!/bin/sh\n"
        'printf \'sh\\t%s\\t%s\\n\' "$1" "$2" >> "$AGW_PROBE_AUDIT"\n'
        'case "$*" in\n'
        '  "-c if test -d /proc/10101; then printf present; else printf absent; fi"|\n'
        '  "-c if test -d /proc/20202; then printf present; else printf absent; fi") ;;\n'
        "  *) exit 97 ;;\n"
        "esac\n"
        "printf present\n"
    )
    shell.chmod(0o755)

    # Socket paths must fit sun_path, which pytest's nested tmp_path can exceed.
    sockets = Path(tempfile.mkdtemp(prefix="agw-batch-", dir="/tmp"))
    try:
        owned = sockets / "owned.sock"
        foreign = sockets / "foreign.sock"
        listeners = []
        for path in (owned, foreign):
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(path))
            listeners.append(listener)
        linked = sockets / "linked.sock"
        linked.symlink_to(owned)
        regular = sockets / "regular.sock"
        regular.write_text("")

        def snapshot() -> dict[str, tuple[str, bytes | str]]:
            observed: dict[str, tuple[str, bytes | str]] = {}
            for path in tmp_path.rglob("*"):
                if path == audit:
                    continue
                relative = str(path.relative_to(tmp_path))
                if path.is_symlink():
                    observed[relative] = ("link", os.readlink(path))
                elif path.is_file():
                    observed[relative] = ("file", path.read_bytes())
                else:
                    observed[relative] = ("directory", "")
            return observed

        before = snapshot()
        sessions = [
            _session("admin", socket_path="/tmp/admin.sock"),
            _session("agent", socket_path="/tmp/agent.sock", mode="agent"),
            _session("admin-pid", pid=10101, socket_path="/tmp/admin-pid.sock"),
            _session("agent-pid", pid=20202, socket_path="/tmp/agent-pid.sock", mode="agent"),
            _session("owned", socket_path=str(owned), mode="agent"),
            _session("foreign", socket_path=str(foreign), mode="agent"),
            _session("linked", socket_path=str(linked), mode="agent"),
            _session("regular", socket_path=str(regular), mode="agent"),
        ]
        owners = {session.name: _owner(session) for session in sessions}
        owners["owned"] = pwd.getpwuid(os.getuid()).pw_name
        environment = os.environ.copy()
        environment.update(
            {
                "AGW_PROBE_AUDIT": str(audit),
                "HOME": str(tmp_path),
                "PATH": str(bin_dir),
                "TMPDIR": str(tmp_path),
            }
        )
        environment.pop("BASH_ENV", None)
        program = (
            "command_not_found_handle() { "
            'printf \'unexpected\\t%s\\n\' "$*" >> "$AGW_PROBE_AUDIT"; return 127; }; '
            f"{_batch_probe_command()}"
        )

        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", program],
            input=_batch_probe_data(sessions, owners),
            text=True,
            capture_output=True,
            check=False,
            cwd=tmp_path,
            env=environment,
            timeout=5,
        )
        for listener in listeners:
            listener.close()
    finally:
        shutil.rmtree(sockets)

    assert result.returncode == 0
    assert result.stderr == ""
    refused = {"foreign", "linked", "regular"}
    frames = {
        base64_name: fields
        for base64_name, fields in ((line.split(":")[1], line.split(":")) for line in result.stdout.splitlines())
    }
    assert {name for name in owners if frames[_encoded_probe_field(name)][2:5:2] == ["126", "126"]} == refused
    parsed = _batch(sessions, admin=_admin(_FakeTarget({"has-session": _FakeResult(stdout=result.stdout)})))
    assert parsed == {
        session.name: SessionStatus.UNKNOWN if session.pid is None else SessionStatus.RUNNING for session in sessions
    }
    assert audit.read_text().splitlines() == [
        "runuser\tadmin",
        "tmux\t-S\t/tmp/admin.sock\thas-session\t-t\t=admin",
        "runuser\tadmin",
        "tmux\t-S\t/tmp/admin.sock\tlist-sessions",
        "runuser\tagt-agent",
        "tmux\t-S\t/tmp/agent.sock\thas-session\t-t\t=agent",
        "runuser\tagt-agent",
        "tmux\t-S\t/tmp/agent.sock\tlist-sessions",
        "runuser\tadmin",
        "tmux\t-S\t/tmp/admin-pid.sock\thas-session\t-t\t=admin-pid",
        "runuser\tadmin",
        "tmux\t-S\t/tmp/admin-pid.sock\tlist-sessions",
        "sh\t-c\tif test -d /proc/10101; then printf present; else printf absent; fi",
        "runuser\tagt-agent-pid",
        "tmux\t-S\t/tmp/agent-pid.sock\thas-session\t-t\t=agent-pid",
        "runuser\tagt-agent-pid",
        "tmux\t-S\t/tmp/agent-pid.sock\tlist-sessions",
        "sh\t-c\tif test -d /proc/20202; then printf present; else printf absent; fi",
        f"runuser\t{owners['owned']}",
        f"tmux\t-S\t{owned}\thas-session\t-t\t=owned",
        f"runuser\t{owners['owned']}",
        f"tmux\t-S\t{owned}\tlist-sessions",
    ]
    assert snapshot() == before


def test_agent_fingerprint_repair_uses_elevation_and_refuses_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An indeterminate cross-user probe never becomes stopped state."""
    from agentworks.sessions.tmux import FingerprintProbe

    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    calls: list[bool] = []

    target = _FakeTarget()

    def capture(*, run_command: Callable[..., object], **kwargs: object) -> FingerprintProbe:
        run_command("probe", check=False)
        calls.append(target.call_options[-1][0])
        return FingerprintProbe(ProbeStatus.UNKNOWN)

    monkeypatch.setattr("agentworks.sessions.tmux.capture_tmux_server_fingerprint", capture)
    monkeypatch.setattr(
        "agentworks.sessions.manager._pids._prove_stored_runtime_absent",
        lambda *args, **kwargs: pytest.fail("indeterminate fingerprint entered absence proof"),
    )

    class _NoMutationDB(_Owners):
        def compare_and_set_session_runtime_repair(self, *args: object, **kwargs: object) -> None:
            pytest.fail("indeterminate repair mutated session runtime state")

    with pytest.raises(StateError):
        _repair_session_pid(_owned(session), admin=_admin(target), db=_NoMutationDB())  # type: ignore[arg-type]

    assert calls == [True]


def test_repair_can_converge_without_pre_table_narration(
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Listing can retain repair side effects while leaving status to its table."""
    from agentworks.sessions.tmux import FingerprintProbe

    session = _session("s1", pid=42, socket_path="/sock", mode="admin", boot_id=BOOT_CURRENT)
    updates: list[dict[str, object]] = []

    monkeypatch.setattr(
        "agentworks.sessions.tmux.capture_tmux_server_fingerprint",
        lambda **kwargs: FingerprintProbe(ProbeStatus.ABSENT),
    )
    monkeypatch.setattr(
        "agentworks.sessions.manager._pids._prove_stored_runtime_absent",
        lambda *args, **kwargs: True,
    )

    class _DB(_Owners):
        def compare_and_set_session_runtime_repair(self, expected: SessionRuntimeSnapshot, **kwargs: object) -> None:
            updates.append({"name": expected.name, **kwargs})

    assert _repair_session_pid(_owned(session), admin=_admin(_FakeTarget()), db=_DB(), announce=False)  # type: ignore[arg-type]
    assert len(updates) == 1
    assert captured_output.lines == []


# -- check_session_status ---------------------------------------------------

BOOT_CURRENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
BOOT_STALE = "11111111-2222-3333-4444-555555555555"


def test_agent_ok() -> None:
    """Agent session: has-session succeeds and reports running."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget({"has-session": _FakeResult(ok=True)})
    assert _check(session, admin=_admin(target)) == SessionStatus.RUNNING


def test_agent_stopped_pid_dead() -> None:
    """Agent session: has-session fails, PID dead -> STOPPED."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
            "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
            "test -d /proc/42": _FakeResult(ok=True, stdout="absent"),
        }
    )
    assert _check(session, admin=_admin(target)) == SessionStatus.STOPPED


def test_agent_stopped_stale_boot() -> None:
    """Agent session: has-session fails, stale boot -> STOPPED (no PID check)."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_STALE)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
            "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
        }
    )
    assert _check(session, admin=_admin(target)) == SessionStatus.STOPPED
    # PID should NOT be checked (stale boot short-circuits)
    assert not any("test -d /proc" in cmd for cmd in target.commands)


def test_agent_broken() -> None:
    """Agent session: has-session fails, same boot, PID alive -> BROKEN."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
            "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
            "test -d /proc/42": _FakeResult(ok=True, stdout="present"),
        }
    )
    assert _check(session, admin=_admin(target)) == SessionStatus.BROKEN


def test_agent_status_elevates_cross_user_pid_probe() -> None:
    """The admin-side singular status path can inspect an agent-owned PID."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)

    class _ElevatedPidTarget(_FakeTarget):
        def __init__(self) -> None:
            super().__init__(
                {
                    "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
                    "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
                    "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
                }
            )
            self.pid_sudo: list[bool] = []

        def run(self, command: str, **kwargs: object) -> _FakeResult:
            if "test -d /proc/42" in command:
                elevated = kwargs.get("sudo") is True
                self.pid_sudo.append(elevated)
                return framed_runtime_result(
                    command,
                    _FakeResult(ok=elevated, returncode=0 if elevated else 2, stdout="present" if elevated else ""),
                )
            return super().run(command, **kwargs)

    target = _ElevatedPidTarget()
    assert _check(session, admin=_admin(target)) == SessionStatus.BROKEN
    assert target.pid_sudo == [True]


def test_agent_status_treats_sudo_refusal_as_unknown() -> None:
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
            "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
            "test -d /proc/42": _FakeResult(ok=False, returncode=1, stderr="sudo: a password is required"),
        }
    )

    assert _check(session, admin=_admin(target)) == SessionStatus.UNKNOWN


def test_agent_repair_does_not_treat_sudo_refusal_as_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.sessions.tmux import FingerprintProbe

    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    monkeypatch.setattr(
        "agentworks.sessions.tmux.capture_tmux_server_fingerprint",
        lambda **kwargs: FingerprintProbe(ProbeStatus.ABSENT),
    )
    target = _FakeTarget(
        {
            "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
            "test -d /proc/42": _FakeResult(ok=False, returncode=1, stderr="sudo: a password is required"),
        }
    )

    class _NoMutationDB(_Owners):
        def compare_and_set_session_runtime_repair(self, *args: object, **kwargs: object) -> None:
            pytest.fail("sudo refusal mutated session runtime state")

    with pytest.raises(ConnectivityError):
        _repair_session_pid(_owned(session), admin=_admin(target), db=_NoMutationDB())  # type: ignore[arg-type]


def test_reachable_server_without_canonical_session_is_residual() -> None:
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=True),
        }
    )
    assert _check(session, admin=_admin(target)) == SessionStatus.RESIDUAL
    assert not any("/proc/42" in command for command in target.commands)


def test_admin_ok() -> None:
    """Admin session: has-session succeeds and reports running."""
    session = _session("s1", pid=42, socket_path="/sock", mode="admin", boot_id=BOOT_CURRENT)
    target = _FakeTarget({"has-session": _FakeResult(ok=True)})
    assert _check(session, admin=_admin(target)) == SessionStatus.RUNNING


def test_admin_stopped_dead_pid() -> None:
    """Admin session: has-session fails AND PID is dead -> STOPPED.

    After the env-and-secrets SDD admin sessions also have per-session
    sockets, so the status check uses the same path as agent sessions
    (BROKEN applies if the PID is alive on the same boot)."""
    session = _session("s1", pid=42, socket_path="/sock", mode="admin", boot_id=BOOT_CURRENT)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
            "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
            "test -d /proc/42": _FakeResult(ok=True, stdout="absent"),
        }
    )
    assert _check(session, admin=_admin(target)) == SessionStatus.STOPPED


def test_admin_broken_after_setenv_pivot() -> None:
    """Admin session: has-session fails AND PID is alive on the same boot
    -> BROKEN. Before the env-and-secrets SDD admin sessions never reached
    BROKEN (they shared the default tmux server, so a PID-alive socket-
    unreachable state didn't exist). With per-session admin sockets the
    same BROKEN semantic that applies to agents now applies to admin."""
    session = _session("s1", pid=42, socket_path="/sock", mode="admin", boot_id=BOOT_CURRENT)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
            "boot_id": _FakeResult(ok=True, stdout=BOOT_CURRENT + "\n"),
            "test -d /proc/42": _FakeResult(ok=True, stdout="present"),
        }
    )
    assert _check(session, admin=_admin(target)) == SessionStatus.BROKEN


def test_legacy_admin_session_without_socket_raises_state_error() -> None:
    """A SessionRow predating the env-and-secrets SDD that has socket_path=None
    surfaces as a typed StateError so the CLI's top-level error wrapper
    renders it cleanly."""
    session = _session("s1", pid=42, mode="admin", boot_id=BOOT_CURRENT)
    target = _FakeTarget({"has-session": _FakeResult(ok=True)})
    with pytest.raises(StateError) as exc:
        _check(session, admin=_admin(target))
    assert exc.value.entity_kind == "session"
    assert exc.value.entity_name == "s1"
    assert exc.value.hint is not None


def test_unknown_no_pid() -> None:
    session = _session("s1", pid=None)
    assert _check(session, admin=_admin(_FakeTarget())) == SessionStatus.UNKNOWN


def test_unknown_no_boot_id() -> None:
    """A legacy row without boot identity remains indeterminate."""
    session = _session("s1", pid=42, boot_id=None)
    assert _check(session, admin=_admin(_FakeTarget())) == SessionStatus.UNKNOWN


def test_missing_start_ticks_alone_does_not_trigger_eager_identity_repair() -> None:
    session = _session("s1", pid=42, socket_path="/sock", boot_id=BOOT_CURRENT)
    assert session.tmux_server_start_ticks is None

    assert not _needs_repair(session)


@pytest.mark.parametrize(
    ("pid", "boot_id", "expected"),
    [
        (None, BOOT_CURRENT, SessionStatus.UNKNOWN),
        (PID_STOPPED, None, SessionStatus.UNKNOWN),
        (42, None, SessionStatus.RUNNING),
    ],
)
def test_present_dedicated_session_requires_a_recorded_pid_for_running_inventory(
    pid: int | None,
    boot_id: str | None,
    expected: SessionStatus,
) -> None:
    session = _session("s1", pid=pid, socket_path="/sock", boot_id=boot_id)
    target = _FakeTarget({"has-session": _FakeResult(ok=True)})

    assert _check(session, admin=_admin(target)) == expected
    assert len(target.commands) == 1


@pytest.mark.parametrize(
    ("pid", "boot_id"),
    [
        (None, BOOT_CURRENT),
        (42, None),
    ],
)
def test_incomplete_dedicated_identity_stays_unknown_when_tmux_is_absent(
    pid: int | None,
    boot_id: str | None,
) -> None:
    session = _session("s1", pid=pid, socket_path="/sock", boot_id=boot_id)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
        }
    )

    assert _check(session, admin=_admin(target)) == SessionStatus.UNKNOWN


def test_stopped_pid_sentinel() -> None:
    from agentworks.db import PID_STOPPED

    session = _session("s1", pid=PID_STOPPED, socket_path="/sock")
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
        }
    )
    assert _check(session, admin=_admin(target)) == SessionStatus.STOPPED


# -- get_tmux_server_pid ----------------------------------------------------


def test_probe_pid_success() -> None:
    target = _FakeTarget({"display-message": _FakeResult(ok=True, stdout="12345\n")})
    assert probe_tmux_server_pid(run_command=target.run).value == 12345


def test_probe_pid_not_running() -> None:
    target = _FakeTarget({"display-message": _FakeResult(ok=False, stderr="no server running on /tmp/tmux")})
    assert probe_tmux_server_pid(run_command=target.run).status is ProbeStatus.ABSENT


def test_probe_pid_with_socket() -> None:
    target = _FakeTarget({"display-message": _FakeResult(ok=True, stdout="99999\n")})
    result = probe_tmux_server_pid(run_command=target.run, socket_path="/run/test.sock")
    assert result.value == 99999
    assert "-S /run/test.sock" in target.commands[0]


# -- batch unknown detection ------------------------------------------------


def test_batch_status_pid_stopped_not_unknown() -> None:
    """Persisted-stopped rows are classified only after live tmux absence."""
    from agentworks.db import PID_STOPPED

    sessions = [
        _session("ok1", pid=100, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT),
        _session("stopped1", pid=PID_STOPPED, socket_path="/stopped", boot_id=BOOT_CURRENT),
    ]
    target = _FakeTarget(
        {
            "has-session": _FakeResult(
                ok=True,
                stdout="\n".join(
                    (
                        _batch_row("ok1"),
                        _batch_row(
                            "stopped1",
                            has_returncode=1,
                            has_diagnostic="can't find session: stopped1",
                            server_returncode=1,
                            server_diagnostic="no server running on /stopped",
                            pid_returncode=1,
                            pid_fact="",
                        ),
                    )
                ),
            )
        }
    )
    result = _batch(sessions, admin=_admin(target))

    assert result["ok1"] == SessionStatus.RUNNING
    assert result["stopped1"] == SessionStatus.STOPPED


# -- _check_dedicated_agent_session edge cases ------------------------------


def test_agent_unknown_when_boot_id_unreadable() -> None:
    """If boot_id can't be read, return UNKNOWN (don't offer --force on unverified PID)."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
            "boot_id": _FakeResult(ok=False, stdout=""),
        }
    )
    assert _check(session, admin=_admin(target)) == SessionStatus.UNKNOWN


@pytest.mark.parametrize(
    "boot_result",
    [
        _FakeResult(stdout="not-a-uuid\n"),
        _FakeResult(returncode=1, stdout=BOOT_CURRENT + "\n"),
    ],
)
def test_agent_unknown_when_observed_boot_identity_is_untrusted(boot_result: _FakeResult) -> None:
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
            "boot_id": boot_result,
        }
    )
    assert _check(session, admin=_admin(target)) == SessionStatus.UNKNOWN


def test_transport_failure_is_unknown_not_absent() -> None:
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget({"has-session": _FakeResult(returncode=255)})

    assert _check(session, admin=_admin(target)) == SessionStatus.UNKNOWN


@pytest.mark.parametrize("value", [0, -1, "bad", True])
def test_stored_process_start_ticks_must_be_a_positive_integer(value: object) -> None:
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    object.__setattr__(session, "tmux_server_start_ticks", value)

    with pytest.raises(StateError):
        _validated_stored_start_ticks(session)


# -- batch_check_status edge cases ------------------------------------------


def test_batch_empty_boot_id_is_unknown() -> None:
    sessions = [
        _session("a1", pid=100, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT),
    ]
    # Agent failure with empty boot_id field
    target = _FakeTarget(
        {
            "has-session": _FakeResult(
                ok=True,
                stdout=(
                    _batch_row(
                        "a1",
                        has_returncode=1,
                        has_diagnostic="can't find session: a1",
                        server_returncode=1,
                        server_diagnostic="no server running on /sock",
                        boot_id="",
                    )
                    + "\n"
                ),
            ),
        }
    )
    result = _batch(sessions, admin=_admin(target))
    assert result["a1"] == SessionStatus.UNKNOWN


def test_batch_sudo_refusal_is_unknown() -> None:
    session = _session("a1", pid=100, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    row = _batch_row(
        "a1",
        has_returncode=1,
        has_diagnostic="can't find session: a1",
        server_returncode=1,
        server_diagnostic="no server running on /sock",
        pid_returncode=1,
        pid_fact="",
    )

    assert (
        _batch([session], admin=_admin(_FakeTarget({"has-session": _FakeResult(stdout=row)})))["a1"]
        is SessionStatus.UNKNOWN
    )


def test_batch_malformed_stored_boot_id_does_not_drop_valid_sibling() -> None:
    sessions = [
        _session("valid", pid=100, socket_path="/valid", mode="agent", boot_id=BOOT_CURRENT),
        _session("invalid", pid=200, socket_path="/invalid", mode="agent", boot_id="not-a-uuid"),
    ]
    output = "\n".join(
        _batch_row(
            name,
            has_returncode=1,
            has_diagnostic=f"can't find session: {name}",
            server_returncode=1,
            server_diagnostic=f"no server running on /{name}",
            pid_fact="absent",
        )
        for name in ("valid", "invalid")
    )

    result = _batch(sessions, admin=_admin(_FakeTarget({"has-session": _FakeResult(stdout=output)})))

    assert result["valid"] is SessionStatus.STOPPED
    assert result["invalid"] is SessionStatus.UNKNOWN


@pytest.mark.parametrize(
    "result",
    [
        _FakeResult(returncode=1, stdout=f"{_batch_row('alpha')}\n{_batch_row('beta')}\n"),
        _FakeResult(stdout=f"{_batch_row('alpha')}\n{_batch_row('beta')}\n", stderr="transport noise"),
        _FakeResult(stdout=f"unframed\n{_batch_row('alpha')}\n{_batch_row('beta')}\n"),
        _FakeResult(stdout=f"{_batch_row('alpha')}\n"),
        _FakeResult(stdout=f"{_batch_row('alpha')}\n{_batch_row('alpha')}\n"),
        _FakeResult(stdout=f"{_batch_row('alpha')}\n{_batch_row('other')}\n"),
        _FakeResult(stdout=f"{_batch_row('alpha')}\nS:{_encoded_probe_field('beta')}:0:not-hex:0::0::0:\n"),
        _FakeResult(stdout=f"{_batch_row('alpha')}\n\n{_batch_row('beta')}\n"),
        _FakeResult(stdout=f"{_batch_row('alpha')}\r\n{_batch_row('beta')}\n"),
        _FakeResult(stdout=f"{_batch_row('alpha')}\n{_batch_row('beta')}\x00\n"),
    ],
)
def test_batch_response_must_be_complete_authoritative_framing(result: _FakeResult) -> None:
    sessions = [_session(name, socket_path=f"/{name}") for name in ("alpha", "beta")]

    assert _batch(sessions, admin=_admin(_FakeTarget({"has-session": result}))) == {
        "alpha": SessionStatus.UNKNOWN,
        "beta": SessionStatus.UNKNOWN,
    }


def test_session_observer_names_each_rows_owner_from_the_database_and_leaves_an_unsafe_socket_path_unread(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.sessions.tmux import AGENT_SOCKET_ROOT

    db.insert_vm("box", site="site", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
    db.insert_agent("bot", "box", "agt-bot")
    db.insert_session("mine", "ws", "default", SessionMode.ADMIN, socket_path=f"{ADMIN_SOCKETS}/mine.sock")
    db.insert_session(
        "theirs",
        "ws",
        "default",
        SessionMode.AGENT,
        agent_name="bot",
        socket_path=f"{AGENT_SOCKET_ROOT}/agt-bot/t.sock",
    )
    # An agent row whose stored socket sits in the admin's directory: probing it as the agent proves nothing.
    db.insert_session(
        "astray", "ws", "default", SessionMode.AGENT, agent_name="bot", socket_path=f"{ADMIN_SOCKETS}/astray.sock"
    )
    sessions = [row for row in (db.get_session(name) for name in ("mine", "theirs", "astray")) if row is not None]
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "agentworks.sessions.manager._identity_access",
        lambda vm, _config, _agent, **_kwargs: SimpleNamespace(vm_name=vm.name),
    )
    probed: list[tuple[list[str], dict[str, str]]] = []

    def batch(rows: list[SessionRow], *, admin: object, owners: dict[str, str]) -> dict[str, SessionStatus]:
        probed.append(([row.name for row in rows], dict(owners)))
        return {row.name: SessionStatus.RUNNING for row in rows}

    monkeypatch.setattr("agentworks.sessions.manager._status.batch_check_status", batch)

    assert observe_session_statuses(sessions, db=db, config=object()) == {
        "mine": SessionStatus.RUNNING,
        "theirs": SessionStatus.RUNNING,
        "astray": SessionStatus.UNKNOWN,
    }
    [(rows, owners)] = probed
    assert rows == ["mine", "theirs"]
    assert {name: owners[name] for name in rows} == {"mine": "agentworks", "theirs": "agt-bot"}
    assert len(captured_output.warnings) == 1
    assert "astray" in captured_output.warnings[0]


@pytest.mark.parametrize(
    "make_error",
    [
        lambda: ConnectivityError("offline"),
        lambda: ExecutionIncomplete("vm-b", Result(Unknown(UnknownReason.DEADLINE))),
        lambda: TrustMismatch("vm-b"),
    ],
    ids=["connectivity", "incomplete", "host-key-mismatch"],
)
def test_session_observer_isolates_one_vm_read_failure_and_says_why_once(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    make_error: Callable[[], Exception],
) -> None:
    sessions: list[SessionRow] = []
    for vm_name in ("vm-a", "vm-b"):
        db.insert_vm(vm_name, site="site", hostname=vm_name)
        db.update_vm_tailscale(vm_name, f"100.64.0.{1 if vm_name == 'vm-a' else 2}")
        workspace_name = f"ws-{vm_name}"
        session_name = f"session-{vm_name}"
        db.insert_workspace(workspace_name, f"/srv/{workspace_name}", vm_name, workspace_name)
        db.insert_session(
            session_name,
            workspace_name,
            "default",
            SessionMode.ADMIN,
            socket_path=f"{ADMIN_SOCKETS}/{session_name}.sock",
        )
        session = db.get_session(session_name)
        assert session is not None
        sessions.append(session)

    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "agentworks.orchestration.activation.activation_gate",
        lambda *_args, **_kwargs: pytest.fail("session observation activated a VM"),
    )
    monkeypatch.setattr(
        "agentworks.secrets.resolver.Resolver.resolve",
        lambda *_args, **_kwargs: pytest.fail("session observation resolved provider secrets"),
    )
    monkeypatch.setattr(
        "agentworks.sessions.manager.ensure_pids_batch",
        lambda *_args, **_kwargs: pytest.fail("session observation repaired runtime identity"),
    )
    monkeypatch.setattr(
        "agentworks.sessions.manager._identity_access",
        lambda vm, _config, _agent, **_kwargs: SimpleNamespace(vm_name=vm.name),
    )

    def batch(rows: list[SessionRow], *, admin: object, **_kwargs: object) -> dict[str, SessionStatus]:
        if admin.vm_name == "vm-b":  # type: ignore[attr-defined]
            raise error
        return {row.name: SessionStatus.RUNNING for row in rows}

    error = make_error()
    monkeypatch.setattr("agentworks.sessions.manager._status.batch_check_status", batch)

    changes_before = db._conn.total_changes  # noqa: SLF001
    assert observe_session_statuses(sessions, db=db, config=object()) == {
        "session-vm-a": SessionStatus.RUNNING,
        "session-vm-b": SessionStatus.UNKNOWN,
    }
    assert db._conn.total_changes == changes_before  # noqa: SLF001
    [warning] = captured_output.warnings
    assert "'vm-b'" in warning and str(error) in warning


def test_session_observer_reads_a_legacy_row_once_within_the_deadline_and_says_why_it_reads_unknown(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from tests.vm_access_support import install_access_recorder

    db.insert_vm("box", site="site", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
    db.insert_session("old", "ws", "default", SessionMode.ADMIN)
    db.update_session_runtime("old", socket_path=None, pid=42, boot_id=BOOT_CURRENT, tmux_server_start_ticks=7)
    session = db.get_session("old")
    assert session is not None
    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", lambda *_args, **_kwargs: None)
    recorder = install_access_recorder(monkeypatch)
    recorder.answer = lambda call: Result(Unknown(UnknownReason.DEADLINE))

    assert observe_session_statuses([session], db=db, config=object(), include_legacy=True) == {
        "old": SessionStatus.UNKNOWN
    }

    [call] = recorder.calls
    assert call.identity == Admin()
    assert isinstance(call.request, Script) and call.request.deadline == GUEST_OBSERVATION_TIMEOUT_SECONDS
    [warning] = captured_output.warnings
    assert "'box'" in warning


@pytest.mark.parametrize(
    "failure",
    ["host-key-missing", "config", "connectivity", "identity-policy", "no-tailscale-ip"],
)
def test_session_observer_reports_a_vm_it_cannot_reach_once_and_observes_the_others(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    failure: str,
) -> None:
    from agentworks.errors import ConfigError
    from agentworks.vms.applied_state import VMSSHIdentityPolicyError

    sessions: list[SessionRow] = []
    for vm_name in ("vm-a", "vm-b"):
        db.insert_vm(vm_name, site="site", hostname=vm_name)
        if vm_name == "vm-a" or failure != "no-tailscale-ip":
            db.update_vm_tailscale(vm_name, f"100.64.0.{1 if vm_name == 'vm-a' else 2}")
        db.insert_workspace(f"ws-{vm_name}", f"/srv/ws-{vm_name}", vm_name, f"ws-{vm_name}")
        db.insert_session(
            f"session-{vm_name}",
            f"ws-{vm_name}",
            "default",
            SessionMode.ADMIN,
            socket_path=f"{ADMIN_SOCKETS}/{vm_name}.sock",
        )
        session = db.get_session(f"session-{vm_name}")
        assert session is not None
        sessions.append(session)

    errors: dict[str, Exception] = {
        "host-key-missing": TrustMissing("vm-b"),
        "config": ConfigError("no usable ssh client"),
        "connectivity": ConnectivityError("offline"),
        "identity-policy": VMSSHIdentityPolicyError("identity outside policy"),
    }

    def require(_db: object, _config: object, vm: object) -> None:
        if vm.name == "vm-b":  # type: ignore[attr-defined]
            raise errors[failure]

    monkeypatch.setattr("agentworks.vms.manager.require_vm_ssh_boundary", require)
    monkeypatch.setattr(
        "agentworks.sessions.manager._identity_access",
        lambda vm, _config, _agent, **_kwargs: SimpleNamespace(vm_name=vm.name),
    )
    monkeypatch.setattr(
        "agentworks.sessions.manager._status.batch_check_status",
        lambda rows, *, admin, **_kwargs: {row.name: SessionStatus.RUNNING for row in rows},
    )

    assert observe_session_statuses(sessions, db=db, config=object()) == {
        "session-vm-a": SessionStatus.RUNNING,
        "session-vm-b": SessionStatus.UNKNOWN,
    }
    [warning] = captured_output.warnings
    assert "'vm-b'" in warning


# -- _ensure_pid strict gate ------------------------------------------------


def test_ensure_pid_raises_on_unresolvable() -> None:
    from agentworks.sessions.manager import _ensure_pid

    session = _owned(_session("s1", pid=None, socket_path="/sock", mode="agent"))

    class _FailTarget:
        def run(self, command, *, check=True, sudo=False, timeout=None):
            # has-session succeeds but display-message fails -> can't recover PID
            if "has-session" in command:
                return _FakeResult(ok=True)
            if "display-message" in command:
                return _FakeResult(ok=False, stdout="")
            return _FakeResult(ok=True)

    class _FakeDb(_Owners):
        def get_session(self, name):
            return session

    with pytest.raises(StateError):
        _ensure_pid(session, admin=_admin(_FailTarget()), db=_FakeDb())


def test_single_session_status_refuses_a_socket_path_outside_its_owners_directory_before_any_read() -> None:
    session = _session("s1", pid=42, socket_path="/tmp/s1.sock", mode="agent", boot_id=BOOT_CURRENT)
    admin = _admin(_FakeTarget())

    with pytest.raises(StateError):
        check_session_status(session, admin=admin, db=_Owners())  # type: ignore[arg-type]

    assert admin.requests() == []


@requires_symlinks
@requires_posix_shell
@pytest.mark.parametrize(
    ("kind", "read"),
    [("owned", True), ("missing", True), ("foreign", False), ("linked", False), ("regular", False)],
)
def test_single_session_tmux_reads_run_as_the_owner_behind_the_socket_guard(kind: str, read: bool) -> None:
    """An agent session's tmux read elevates only to run as its owner, and never reaches a socket it cannot trust."""
    import pwd
    import socket
    import tempfile
    from pathlib import Path

    from agentworks.sessions.manager._status import owner_run_command
    from agentworks.sessions.manager._teardown import ManagedSocket
    from agentworks.sessions.tmux import probe_tmux_session

    me = pwd.getpwuid(os.getuid()).pw_name
    root = Path(tempfile.mkdtemp(prefix="agw-owner-", dir="/tmp"))
    try:
        bin_dir, audit = root / "bin", root / "audit"
        bin_dir.mkdir()
        for name in ("sh", "stat"):
            executable = shutil.which(name)
            assert executable is not None
            os.symlink(executable, bin_dir / name)
        (bin_dir / "runuser").write_text(
            '#!/bin/sh\n[ "$1" = -u ] && [ "$3" = -- ] || exit 64\n'
            f'printf "runuser %s\\n" "$2" >> {audit}\nshift 3\nexec "$@"\n'
        )
        (bin_dir / "tmux").write_text(
            f'#!/bin/sh\nprintf "tmux %s\\n" "$*" >> {audit}\nprintf "no server running on %s\\n" "$2" >&2\nexit 1\n'
        )
        for shim in ("runuser", "tmux"):
            (bin_dir / shim).chmod(0o755)
        path = root / "s.sock"
        listener = socket.socket(socket.AF_UNIX)
        if kind in {"owned", "foreign"}:
            listener.bind(str(path))
        elif kind == "linked":
            listener.bind(str(root / "real.sock"))
            path.symlink_to(root / "real.sock")
        elif kind == "regular":
            path.write_text("")

        def run_here(call: Call) -> Result:
            request = call.request
            assert isinstance(request, Script) and request.elevated
            completed = subprocess.run(
                ["/bin/sh", "-c", request.source], capture_output=True, env={"PATH": str(bin_dir)}, check=False
            )
            return Result(Completed(completed.returncode), completed.stdout, completed.stderr)

        admin = RecordingVMAccess([], Admin())
        admin.answer = run_here
        session = _session("s1", pid=42, socket_path=str(path), mode="agent", boot_id=BOOT_CURRENT)
        owner = "agt-someone-else" if kind == "foreign" else me
        presence = probe_tmux_session(
            "s1", run_command=owner_run_command(admin, session, ManagedSocket(str(path), owner)), socket_path=str(path)
        )
        listener.close()
        calls = audit.read_text().splitlines() if audit.exists() else []
    finally:
        shutil.rmtree(root)

    if read:
        assert presence is ProbeStatus.ABSENT
        assert calls == [f"runuser {me}", f"tmux -S {path} has-session -t =s1"]
    else:
        assert presence is ProbeStatus.UNKNOWN
        assert calls == []
