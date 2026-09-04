"""Tests for session liveness checking functions."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import SessionMode, SessionRow, SessionStatus
from agentworks.errors import ConnectivityError, NotFoundError, StateError
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
    _BoundedStatusTarget,
    _encoded_probe_field,
)
from agentworks.sessions.tmux import ProbeStatus, probe_tmux_server_pid

if TYPE_CHECKING:
    from pathlib import Path


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
    """Fake ``Transport`` that returns canned responses keyed by substring match."""

    def __init__(self, responses: dict[str, _FakeResult] | None = None) -> None:
        self._responses = responses or {}
        self.commands: list[str] = []
        self.call_options: list[tuple[bool | None, int | None, int | None]] = []

    def run(
        self,
        command: str,
        *,
        check: bool = True,
        sudo: bool = False,
        tty: bool | None = None,
        timeout: int | None = None,
        retries: int | None = None,
        input_data: str | None = None,
    ) -> _FakeResult:
        self.commands.append(command)
        self.call_options.append((tty, timeout, retries))
        for pattern, result in self._responses.items():
            if pattern in command:
                return result
        return _FakeResult()


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
    result = batch_check_status(sessions, target=target)
    assert result["a1"] == SessionStatus.RUNNING
    assert result["s1"] == SessionStatus.STOPPED
    assert "s2" not in result


def test_batch_empty() -> None:
    assert batch_check_status([], target=_FakeTarget()) == {}


def test_batch_all_missing_pid() -> None:
    sessions = [_session("s1", pid=None)]
    assert batch_check_status(sessions, target=_FakeTarget()) == {}


def test_session_observer_preserves_missing_workspace_as_structural_failure() -> None:
    class _DB:
        def get_workspace(self, _name: str) -> None:
            return None

    with pytest.raises(NotFoundError) as caught:
        observe_session_statuses([_session("s1", socket_path="/sock")], db=_DB(), config=object())  # type: ignore[arg-type]
    assert caught.value.entity_kind == "workspace"
    assert caught.value.entity_name == "ws"


def test_session_observer_preserves_corrupt_ssh_applied_state(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("box", site="site", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.insert_workspace("ws", "/srv/ws", "box", "ws-ws")
    db.insert_session("alpha", "ws", "default", SessionMode.ADMIN, socket_path="/tmp/alpha.sock")
    session = db.get_session("alpha")
    assert session is not None
    structural = StateError("corrupt SSH applied state", entity_kind="vm", entity_name="box")
    monkeypatch.setattr(
        "agentworks.vms.manager.require_vm_ssh_boundary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(structural),
    )
    monkeypatch.setattr(
        "agentworks.sessions.manager.transport",
        lambda *_args, **_kwargs: pytest.fail("transport constructed after structural failure"),
    )

    with pytest.raises(StateError) as caught:
        observe_session_statuses([session], db=db, config=object())

    assert caught.value is structural


def test_batch_builds_compound_command() -> None:
    """Compound command includes has-session for both agent and admin sessions."""
    sessions = [
        _session("a1", pid=100, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT),
        _session("s1", pid=200, socket_path="/sock2", mode="admin", boot_id=BOOT_CURRENT),
    ]
    target = _FakeTarget({"has-session": _FakeResult(ok=True, stdout=f"{_batch_row('a1')}\n{_batch_row('s1')}\n")})
    batch_check_status(sessions, target=target)
    assert len(target.commands) == 1
    assert target.call_options == [(False, 10, 1)]
    assert 'if [ "$M" = a ]; then sudo -n tmux' in target.commands[0]
    assert 'sudo -n sh -c "if test -d /proc/$PID' in target.commands[0]


def test_bounded_singular_status_target_closes_stdin() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Target:
        def run(self, command: str, **kwargs: object) -> _FakeResult:
            calls.append((command, kwargs))
            return _FakeResult()

    _BoundedStatusTarget(Target()).run("true")  # type: ignore[arg-type]

    assert calls == [
        (
            "true",
            {
                "timeout": 10,
                "retries": 1,
                "tty": False,
                "input_data": "",
            },
        )
    ]


def test_batch_fleet_uses_constant_argv_and_unbounded_stdin_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.sessions.tmux import MAX_SESSION_NAME_LENGTH, SUN_PATH_MAX
    from agentworks.transports import SSHTransport

    sessions = []
    for index in range(512):
        suffix = f"{index:08x}"
        name = ("s" * (MAX_SESSION_NAME_LENGTH - len(suffix))) + suffix
        fixed = f"/{suffix}/{name}"
        socket_path = "/" + ("p" * (SUN_PATH_MAX - 2 - len(fixed))) + fixed
        assert len(socket_path) == SUN_PATH_MAX - 1
        sessions.append(_session(name, pid=100_000 + index, socket_path=socket_path, mode="agent"))

    completed = subprocess.CompletedProcess(
        [],
        0,
        stdout=("\n".join(_batch_row(session.name) for session in sessions) + "\n").encode(),
        stderr=b"",
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run_process(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return completed

    monkeypatch.setattr("agentworks.transports.ssh.subprocess.run", run_process)

    status_map = batch_check_status(sessions, target=SSHTransport("vm-host"))

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[-1] == _batch_probe_command()
    assert len(" ".join(argv).encode()) < 32_767
    payload = kwargs["input"]
    assert payload == _batch_probe_data(sessions).encode()
    assert isinstance(payload, bytes)
    assert len(payload) > 32_767
    assert "-T" in argv
    assert status_map == {session.name: SessionStatus.RUNNING for session in sessions}


def test_batch_command_is_valid_shell_syntax() -> None:
    command = _batch_probe_command()

    result = subprocess.run(["bash", "-n", "-c", command], check=False, capture_output=True)

    assert result.returncode == 0


def test_batch_probe_executes_only_read_only_guest_operations(tmp_path: Path) -> None:
    """Reject unexpected PATH tools, sudo calls, tmux verbs, and temp-tree writes."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    audit = tmp_path / "audit"

    for name in ("base64", "cat", "od", "tr"):
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

    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/bin/sh\n"
        'if [ "$#" -lt 2 ] || [ "$1" != -n ]; then\n'
        '  printf \'unexpected\\tsudo\\t%s\\n\' "$*" >> "$AGW_PROBE_AUDIT"\n'
        "  exit 97\n"
        "fi\n"
        'case "$2" in tmux|sh) ;; *)\n'
        '  printf \'unexpected\\tsudo\\t%s\\n\' "$*" >> "$AGW_PROBE_AUDIT"\n'
        "  exit 97\n"
        "esac\n"
        "shift\n"
        'exec "$@"\n'
    )
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
    ]
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
        input=_batch_probe_data(sessions),
        text=True,
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=environment,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    parsed = batch_check_status(sessions, target=_FakeTarget({"has-session": _FakeResult(stdout=result.stdout)}))
    assert parsed == {session.name: SessionStatus.RUNNING for session in sessions}
    assert audit.read_text().splitlines() == [
        "tmux\t-S\t/tmp/admin.sock\thas-session\t-t\t=admin",
        "tmux\t-S\t/tmp/admin.sock\tlist-sessions",
        "tmux\t-S\t/tmp/agent.sock\thas-session\t-t\t=agent",
        "tmux\t-S\t/tmp/agent.sock\tlist-sessions",
        "tmux\t-S\t/tmp/admin-pid.sock\thas-session\t-t\t=admin-pid",
        "tmux\t-S\t/tmp/admin-pid.sock\tlist-sessions",
        "sh\t-c\tif test -d /proc/10101; then printf present; else printf absent; fi",
        "tmux\t-S\t/tmp/agent-pid.sock\thas-session\t-t\t=agent-pid",
        "tmux\t-S\t/tmp/agent-pid.sock\tlist-sessions",
        "sh\t-c\tif test -d /proc/20202; then printf present; else printf absent; fi",
    ]
    assert snapshot() == before


def test_batch_accepts_exact_ssh_close_advisory_after_complete_frames() -> None:
    session = _session("alpha", pid=42, socket_path="/tmp/alpha.sock")
    target = _FakeTarget(
        {
            "has-session": _FakeResult(
                stdout=f"{_batch_row('alpha')}\n",
                stderr="Shared connection to vm.example closed.\n",
            )
        }
    )

    assert batch_check_status([session], target=target) == {"alpha": SessionStatus.RUNNING}


def test_agent_fingerprint_repair_uses_elevation_and_refuses_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An indeterminate cross-user probe never becomes stopped state."""
    from agentworks.sessions.tmux import FingerprintProbe

    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    calls: list[bool] = []

    def capture(**kwargs: object) -> FingerprintProbe:
        calls.append(kwargs.get("sudo") is True)
        return FingerprintProbe(ProbeStatus.UNKNOWN)

    monkeypatch.setattr("agentworks.sessions.tmux.capture_tmux_server_fingerprint", capture)
    monkeypatch.setattr(
        "agentworks.sessions.manager._pids._prove_stored_runtime_absent",
        lambda *args, **kwargs: pytest.fail("indeterminate fingerprint entered absence proof"),
    )

    class _NoMutationDB:
        def update_session_runtime(self, *args: object, **kwargs: object) -> None:
            pytest.fail("indeterminate repair mutated session runtime state")

    with pytest.raises(StateError):
        _repair_session_pid(session, target=_FakeTarget(), db=_NoMutationDB())  # type: ignore[arg-type]

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

    class _DB:
        def update_session_runtime(self, name: str, **kwargs: object) -> None:
            updates.append({"name": name, **kwargs})

    assert _repair_session_pid(session, target=_FakeTarget(), db=_DB(), announce=False)  # type: ignore[arg-type]
    assert len(updates) == 1
    assert captured_output.lines == []


# -- check_session_status ---------------------------------------------------

BOOT_CURRENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
BOOT_STALE = "11111111-2222-3333-4444-555555555555"


def test_agent_ok() -> None:
    """Agent session: has-session succeeds and reports running."""
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget({"has-session": _FakeResult(ok=True)})
    assert check_session_status(session, target=target) == SessionStatus.RUNNING


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
    assert check_session_status(session, target=target) == SessionStatus.STOPPED


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
    assert check_session_status(session, target=target) == SessionStatus.STOPPED
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
    assert check_session_status(session, target=target) == SessionStatus.BROKEN


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
                return _FakeResult(ok=elevated, returncode=0 if elevated else 2, stdout="present" if elevated else "")
            return super().run(command, **kwargs)

    target = _ElevatedPidTarget()
    assert check_session_status(session, target=target) == SessionStatus.BROKEN
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

    assert check_session_status(session, target=target) == SessionStatus.UNKNOWN


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

    class _NoMutationDB:
        def update_session_runtime(self, *args: object, **kwargs: object) -> None:
            pytest.fail("sudo refusal mutated session runtime state")

    with pytest.raises(ConnectivityError):
        _repair_session_pid(session, target=target, db=_NoMutationDB())  # type: ignore[arg-type]


def test_reachable_server_without_canonical_session_is_residual() -> None:
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=True),
        }
    )
    assert check_session_status(session, target=target) == SessionStatus.RESIDUAL
    assert not any("/proc/42" in command for command in target.commands)


def test_admin_ok() -> None:
    """Admin session: has-session succeeds and reports running."""
    session = _session("s1", pid=42, socket_path="/sock", mode="admin", boot_id=BOOT_CURRENT)
    target = _FakeTarget({"has-session": _FakeResult(ok=True)})
    assert check_session_status(session, target=target) == SessionStatus.RUNNING


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
    assert check_session_status(session, target=target) == SessionStatus.STOPPED


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
    assert check_session_status(session, target=target) == SessionStatus.BROKEN


def test_legacy_admin_session_without_socket_raises_state_error() -> None:
    """A SessionRow predating the env-and-secrets SDD that has socket_path=None
    surfaces as a typed StateError so the CLI's top-level error wrapper
    renders it cleanly."""
    session = _session("s1", pid=42, mode="admin", boot_id=BOOT_CURRENT)
    target = _FakeTarget({"has-session": _FakeResult(ok=True)})
    with pytest.raises(StateError) as exc:
        check_session_status(session, target=target)
    assert exc.value.entity_kind == "session"
    assert exc.value.entity_name == "s1"
    assert exc.value.hint is not None


def test_unknown_no_pid() -> None:
    session = _session("s1", pid=None)
    assert check_session_status(session, target=_FakeTarget()) == SessionStatus.UNKNOWN


def test_unknown_no_boot_id() -> None:
    """A legacy row without boot identity remains indeterminate."""
    session = _session("s1", pid=42, boot_id=None)
    assert check_session_status(session, target=_FakeTarget()) == SessionStatus.UNKNOWN


def test_missing_start_ticks_alone_does_not_trigger_eager_identity_repair() -> None:
    session = _session("s1", pid=42, socket_path="/sock", boot_id=BOOT_CURRENT)
    assert session.tmux_server_start_ticks is None

    assert not _needs_repair(session)


@pytest.mark.parametrize(
    ("pid", "boot_id"),
    [
        (None, BOOT_CURRENT),
        (42, None),
    ],
)
def test_incomplete_dedicated_identity_is_live_when_exact_tmux_session_is_present(
    pid: int | None,
    boot_id: str | None,
) -> None:
    session = _session("s1", pid=pid, socket_path="/sock", boot_id=boot_id)
    target = _FakeTarget({"has-session": _FakeResult(ok=True)})

    assert check_session_status(session, target=target) == SessionStatus.RUNNING
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

    assert check_session_status(session, target=target) == SessionStatus.UNKNOWN


def test_stopped_pid_sentinel() -> None:
    from agentworks.db import PID_STOPPED

    session = _session("s1", pid=PID_STOPPED, socket_path="/sock")
    target = _FakeTarget(
        {
            "has-session": _FakeResult(ok=False, stderr="can't find session: s1"),
            "list-sessions": _FakeResult(ok=False, stderr="no server running on /sock"),
        }
    )
    assert check_session_status(session, target=target) == SessionStatus.STOPPED


# -- get_tmux_server_pid ----------------------------------------------------


def test_probe_pid_success() -> None:
    target = _FakeTarget({"display-message": _FakeResult(ok=True, stdout="12345\n")})
    assert probe_tmux_server_pid(target=target).value == 12345


def test_probe_pid_not_running() -> None:
    target = _FakeTarget({"display-message": _FakeResult(ok=False, stderr="no server running on /tmp/tmux")})
    assert probe_tmux_server_pid(target=target).status is ProbeStatus.ABSENT


def test_probe_pid_with_socket() -> None:
    target = _FakeTarget({"display-message": _FakeResult(ok=True, stdout="99999\n")})
    result = probe_tmux_server_pid(target=target, socket_path="/run/test.sock")
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
    result = batch_check_status(sessions, target=target)

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
    assert check_session_status(session, target=target) == SessionStatus.UNKNOWN


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
    assert check_session_status(session, target=target) == SessionStatus.UNKNOWN


def test_transport_failure_is_unknown_not_absent() -> None:
    session = _session("s1", pid=42, socket_path="/sock", mode="agent", boot_id=BOOT_CURRENT)
    target = _FakeTarget({"has-session": _FakeResult(returncode=255)})

    assert check_session_status(session, target=target) == SessionStatus.UNKNOWN


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
    result = batch_check_status(sessions, target=target)
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
        batch_check_status([session], target=_FakeTarget({"has-session": _FakeResult(stdout=row)}))["a1"]
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

    result = batch_check_status(sessions, target=_FakeTarget({"has-session": _FakeResult(stdout=output)}))

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

    assert batch_check_status(sessions, target=_FakeTarget({"has-session": result})) == {
        "alpha": SessionStatus.UNKNOWN,
        "beta": SessionStatus.UNKNOWN,
    }


def test_session_observer_isolates_one_vm_transport_failure(
    db,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
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
            socket_path=f"/tmp/{session_name}.sock",
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
        "agentworks.sessions.manager.transport",
        lambda vm, _config, **_kwargs: SimpleNamespace(vm_name=vm.name),
    )

    def batch(rows: list[SessionRow], *, target: object, **_kwargs: object) -> dict[str, SessionStatus]:
        if target.vm_name == "vm-b":  # type: ignore[attr-defined]
            raise ConnectivityError("offline")
        return {row.name: SessionStatus.RUNNING for row in rows}

    monkeypatch.setattr("agentworks.sessions.manager._status.batch_check_status", batch)

    changes_before = db._conn.total_changes  # noqa: SLF001
    assert observe_session_statuses(sessions, db=db, config=object()) == {
        "session-vm-a": SessionStatus.RUNNING,
        "session-vm-b": SessionStatus.UNKNOWN,
    }
    assert db._conn.total_changes == changes_before  # noqa: SLF001


# -- _ensure_pid strict gate ------------------------------------------------


def test_ensure_pid_raises_on_unresolvable() -> None:
    from agentworks.sessions.manager import _ensure_pid

    session = _session("s1", pid=None, socket_path="/sock", mode="agent")

    class _FailTarget:
        def run(self, command, *, check=True, sudo=False, tty=None, timeout=None):
            # has-session succeeds but display-message fails -> can't recover PID
            if "has-session" in command:
                return _FakeResult(ok=True)
            if "display-message" in command:
                return _FakeResult(ok=False, stdout="")
            return _FakeResult(ok=True)

    class _FakeDb:
        def get_session(self, name):
            return session

    with pytest.raises(StateError):
        _ensure_pid(session, target=_FailTarget(), db=_FakeDb())
