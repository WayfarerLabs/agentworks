"""Session liveness checks (single and batched)."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import agentworks.sessions.manager as _mgr
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus
from agentworks.errors import (
    AgentworksError,
    ConfigError,
    ConnectivityError,
    StateError,
    UserAbort,
)
from agentworks.sessions.tmux import ProbeStatus, probe_tmux_server, probe_tmux_session

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.transports import Transport

_PID_PRESENT_FACT = "present"
_PID_ABSENT_FACT = "absent"
_OBSERVATION_TIMEOUT_SECONDS = 10


class _BoundedStatusTarget:
    """Force singular inspection calls onto the batch probe's safe policy."""

    def __init__(self, target: Transport) -> None:
        self._target = target

    def run(self, command: str, **kwargs: Any) -> Any:
        kwargs.update(timeout=_OBSERVATION_TIMEOUT_SECONDS, retries=1, tty=False)
        return self._target.run(command, **kwargs)


def _pid_probe_script(pid: int) -> str:
    """Return a shell probe whose fact exists only after the shell started."""
    return f"if test -d /proc/{pid}; then printf {_PID_PRESENT_FACT}; else printf {_PID_ABSENT_FACT}; fi"


def _pid_presence_from_fact(returncode: object, fact: object, *, stderr: object = "") -> ProbeStatus:
    if returncode != 0 or not isinstance(fact, str) or not isinstance(stderr, str):
        return ProbeStatus.UNKNOWN
    if fact == _PID_PRESENT_FACT and not stderr:
        return ProbeStatus.PRESENT
    if fact == _PID_ABSENT_FACT and not stderr:
        return ProbeStatus.ABSENT
    return ProbeStatus.UNKNOWN


def _pid_presence(pid: int, *, target: Transport, sudo: bool = False) -> ProbeStatus:
    """Probe a PID without treating a transport failure as process absence."""
    from agentworks.sessions.tmux import _normalized_probe_streams

    result = target.run(_pid_probe_script(pid), sudo=sudo, check=False)
    stdout, stderr = _normalized_probe_streams(result)
    return _pid_presence_from_fact(getattr(result, "returncode", None), stdout, stderr=stderr)


def _get_boot_id(target: Transport) -> str | None:
    """Read the current VM boot ID. Returns None on failure."""
    from agentworks.sessions.tmux import canonical_boot_id

    result = target.run("cat /proc/sys/kernel/random/boot_id", check=False)
    if getattr(result, "returncode", 0 if getattr(result, "ok", False) else 1) != 0:
        return None
    return canonical_boot_id(getattr(result, "stdout", ""))


def check_session_status(
    session: SessionRow,
    *,
    target: Transport | None = None,
) -> SessionStatus:
    """Determine session status. Dispatches by session type.

    No DB side effects. Raises ``StateError`` when the session row predates
    the per-session-socket model introduced by the env-and-secrets SDD
    (``socket_path is None`` for an admin session). The hint points the
    operator at ``agw session restart <name>``, which migrates the row to
    the new shape via a surgical kill of the named session on the default
    tmux server + a fresh ``create_tmux_session`` under a per-session
    socket. Callers that aren't a launch operation (attach, stop, etc.)
    can't safely migrate, so they surface the typed error and let the
    operator restart.
    """
    if session.socket_path is not None:
        if target is None:
            raise TypeError("dedicated session status requires a transport")
        return _check_dedicated_session(session, target=target)
    if session.pid is None or session.boot_id is None:
        return SessionStatus.UNKNOWN
    raise _legacy_session_status_error(session)


def _legacy_session_status_error(session: SessionRow) -> StateError:
    """Build the canonical refusal for a live legacy shared-server row."""
    return StateError(
        f"session '{session.name}' has no socket_path",
        entity_kind="session",
        entity_name=session.name,
        hint=(
            "This session predates the per-session-socket model introduced by "
            f"the env-and-secrets SDD. Run `agw session restart {session.name}` "
            "to migrate it to the new shape."
        ),
    )


def _check_dedicated_session(session: SessionRow, *, target: Transport) -> SessionStatus:
    """Sessions with their own tmux server and socket. Applies uniformly to
    admin and agent sessions after the env-and-secrets SDD migrated admin
    sessions to per-session sockets.
    """
    socket_path = session.socket_path
    assert socket_path is not None
    session_presence = probe_tmux_session(session.name, run_command=target.run, socket_path=socket_path)
    if session_presence is ProbeStatus.UNKNOWN:
        return SessionStatus.UNKNOWN
    if session_presence is ProbeStatus.PRESENT:
        return SessionStatus.RUNNING

    server_presence = probe_tmux_server(run_command=target.run, socket_path=socket_path)
    if server_presence is ProbeStatus.UNKNOWN:
        return SessionStatus.UNKNOWN
    if server_presence is ProbeStatus.PRESENT:
        return SessionStatus.RESIDUAL

    # Exact session and dedicated server are authoritatively absent. The
    # stopped sentinel is supporting evidence only after those live facts.
    if session.pid == PID_STOPPED:
        return SessionStatus.STOPPED

    # has-session failed: STOPPED or BROKEN?
    if session.pid is None or session.pid <= 0 or session.boot_id is None:
        return SessionStatus.UNKNOWN
    current_boot = _mgr._get_boot_id(target)
    if current_boot is None:
        return SessionStatus.UNKNOWN  # can't verify boot cycle, unsafe to offer --force
    stored_boot_id = _mgr._validated_stored_boot_id(session)
    if stored_boot_id != current_boot:
        return SessionStatus.STOPPED  # stale boot, PID is meaningless
    pid_presence = _pid_presence(
        session.pid,
        target=target,
        sudo=session.mode == SessionMode.AGENT.value,
    )
    if pid_presence is ProbeStatus.UNKNOWN:
        return SessionStatus.UNKNOWN
    if pid_presence is ProbeStatus.ABSENT:
        return SessionStatus.STOPPED  # process is dead
    return SessionStatus.BROKEN  # same boot, process alive, socket unreachable


def batch_check_status(
    sessions: list[SessionRow],
    *,
    target: Transport,
) -> dict[str, SessionStatus]:
    """Check status for multiple sessions in one SSH call per VM.

    Returns {session_name: SessionStatus}. Dedicated rows are probed even when
    their persisted runtime identity is incomplete: exact tmux presence is
    sufficient for ``RUNNING``; absence remains ``UNKNOWN`` until the stored
    identity can prove more. Persisted-stopped rows are still probed so a
    manually resurrected exact session cannot be hidden by stale evidence.
    """
    from agentworks.sessions.tmux import canonical_boot_id

    checkable = [s for s in sessions if s.socket_path is not None]
    if not checkable:
        return {}

    cmd = _batch_probe_command(checkable)
    result = target.run(
        cmd,
        check=False,
        timeout=_OBSERVATION_TIMEOUT_SECONDS,
        retries=1,
        tty=False,
    )
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""

    status_map: dict[str, SessionStatus] = {
        session.name: SessionStatus.UNKNOWN for session in checkable if session.socket_path is not None
    }
    sessions_by_key = {_encoded_probe_field(session.name): session for session in checkable}

    if result.returncode != 0 or stderr or "\x00" in stdout or "\r" in stdout:
        return status_map

    frames: dict[str, list[str]] = {}
    framed_stdout = stdout[:-1] if stdout.endswith("\n") else stdout
    lines = framed_stdout.split("\n") if framed_stdout else []
    if any(not line for line in lines):
        return status_map
    for line in lines:
        fields = line.split(":")
        if len(fields) != 10:
            return status_map
        key = fields[1]
        if fields[0] != "S" or key not in sessions_by_key or key in frames or not _valid_batch_frame(fields):
            return status_map
        frames[key] = fields
    if set(frames) != set(sessions_by_key):
        return status_map

    for key, fields in frames.items():
        session = sessions_by_key[key]
        name = session.name

        has_presence = _batch_tmux_presence(fields[2], fields[3], missing_target_is_absent=True)
        if has_presence is ProbeStatus.PRESENT:
            status_map[name] = SessionStatus.RUNNING
            continue
        if has_presence is ProbeStatus.UNKNOWN:
            continue

        server_presence = _batch_tmux_presence(fields[4], fields[5], missing_target_is_absent=False)
        if server_presence is ProbeStatus.PRESENT:
            status_map[name] = SessionStatus.RESIDUAL
            continue
        if server_presence is ProbeStatus.UNKNOWN:
            continue

        if session.pid == PID_STOPPED:
            status_map[name] = SessionStatus.STOPPED
            continue

        if session.pid is None or session.pid <= 0 or session.boot_id is None:
            continue

        try:
            boot_returncode = int(fields[6])
            observed_boot = bytes.fromhex(fields[7]).decode("ascii")
            pid_returncode = int(fields[8])
            pid_fact = bytes.fromhex(fields[9]).decode("ascii")
            stored_boot = _mgr._validated_stored_boot_id(session)
        except (StateError, UnicodeDecodeError, ValueError):
            continue
        current_boot = canonical_boot_id(observed_boot) if boot_returncode == 0 else None
        if current_boot is None:
            continue
        if stored_boot != current_boot:
            status_map[name] = SessionStatus.STOPPED
            continue
        pid_presence = _pid_presence_from_fact(pid_returncode, pid_fact)
        if pid_presence is ProbeStatus.UNKNOWN:
            continue
        if pid_presence is ProbeStatus.ABSENT:
            status_map[name] = SessionStatus.STOPPED
        else:
            status_map[name] = SessionStatus.BROKEN

    return status_map


def _encoded_probe_field(value: str) -> str:
    """Encode one row field without shell or frame delimiters."""
    return base64.b64encode(value.encode()).decode("ascii")


def _batch_probe_command(sessions: list[SessionRow]) -> str:
    """Build one compact loop program for a VM's selected session rows."""
    records = []
    for session in sessions:
        assert session.socket_path is not None
        mode = "a" if session.mode == SessionMode.AGENT.value else "u"
        pid = (
            str(session.pid)
            if isinstance(session.pid, int) and not isinstance(session.pid, bool) and session.pid > 0
            else "-"
        )
        records.append(
            " ".join(
                (
                    _encoded_probe_field(session.name),
                    _encoded_probe_field(session.socket_path),
                    mode,
                    pid,
                )
            )
        )
    data = "\n".join(records)
    return (
        "hex() { od -An -tx1 | tr -d ' \\n'; }; "
        'decode() { printf %s "$1" | base64 -d; }; '
        "BOOT=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null); B=$?; "
        'BOOT_HEX=$(printf %s "$BOOT" | hex); '
        "while read -r N64 S64 M PID; do "
        'N=$(decode "$N64") || exit 90; SOCK=$(decode "$S64") || exit 90; '
        'run_tmux() { if [ "$M" = a ]; then sudo -n tmux -S "$SOCK" "$@"; '
        'else tmux -S "$SOCK" "$@"; fi; }; '
        'HERR=$(run_tmux has-session -t "=$N" 2>&1 >/dev/null); H=$?; '
        'HHEX=$(printf %s "$HERR" | hex); '
        "SERR=$(run_tmux list-sessions 2>&1 >/dev/null); S=$?; "
        'SHEX=$(printf %s "$SERR" | hex); '
        'if [ "$PID" = - ]; then PFACT=; P=1; '
        'elif [ "$M" = a ]; then '
        'PFACT=$(sudo -n sh -c "if test -d /proc/$PID; then printf present; '
        'else printf absent; fi" 2>/dev/null); P=$?; '
        'else PFACT=$(sh -c "if test -d /proc/$PID; then printf present; '
        'else printf absent; fi" 2>/dev/null); P=$?; fi; '
        'PHEX=$(printf %s "$PFACT" | hex); '
        'printf "S:%s:%s:%s:%s:%s:%s:%s:%s\\n" '
        '"$N64" "$H" "$HHEX" "$S" "$SHEX" "$B" "$BOOT_HEX" "$P" "$PHEX"; '
        "done <<'AGW_STATUS_ROWS'\n"
        f"{data}\n"
        "AGW_STATUS_ROWS"
    )


def _valid_batch_frame(fields: list[str]) -> bool:
    """Reject malformed framing before any requested row is trusted."""
    try:
        for index in (2, 4, 6, 8):
            int(fields[index])
        bytes.fromhex(fields[3]).decode("utf-8")
        bytes.fromhex(fields[5]).decode("utf-8")
        bytes.fromhex(fields[7]).decode("ascii")
        bytes.fromhex(fields[9]).decode("ascii")
    except (UnicodeDecodeError, ValueError):
        return False
    return True


def _batch_tmux_presence(
    returncode: str,
    diagnostic_hex: str,
    *,
    missing_target_is_absent: bool,
) -> ProbeStatus:
    """Decode one batch fact and delegate classification to tmux authority."""
    from agentworks.sessions.tmux import _tmux_presence_from_result

    try:
        code = int(returncode)
        diagnostic = bytes.fromhex(diagnostic_hex).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return ProbeStatus.UNKNOWN
    result = SimpleNamespace(returncode=code, stdout="", stderr=diagnostic)
    return _tmux_presence_from_result(result, missing_target_is_absent=missing_target_is_absent)


def observe_session_statuses(
    sessions: list[SessionRow],
    *,
    db: Database,
    config: Config,
) -> dict[str, SessionStatus]:
    """Observe every selected session, grouped and bounded by backing VM."""
    from concurrent.futures import as_completed
    from functools import partial

    from agentworks.status_observation import cancelling_futures
    from agentworks.vms.applied_state import VMSSHIdentityPolicyError

    # Resolve each session's VM and group
    by_vm: dict[str, list[SessionRow]] = {}
    vm_targets: dict[str, Transport] = {}
    unavailable_vms: set[str] = set()
    result_map = {session.name: SessionStatus.UNKNOWN for session in sessions}

    for s in sessions:
        ws = _mgr._require_workspace(db, s.workspace_name)
        vm = _mgr._require_vm_for_workspace(db, ws)
        if ws.vm_name in unavailable_vms:
            continue
        if ws.vm_name not in vm_targets:
            if not vm.tailscale_host:
                continue
            try:
                from agentworks.vms.manager import require_vm_ssh_boundary

                require_vm_ssh_boundary(db, config, vm)
            except UserAbort:
                raise
            except (ConfigError, ConnectivityError, VMSSHIdentityPolicyError):
                unavailable_vms.add(ws.vm_name)
                continue
            try:
                vm_targets[ws.vm_name] = _mgr.transport(
                    vm,
                    config,
                    default_timeout=_OBSERVATION_TIMEOUT_SECONDS,
                )
            except UserAbort:
                raise
            except (ConnectivityError, StateError):
                unavailable_vms.add(ws.vm_name)
                continue
        by_vm.setdefault(ws.vm_name, []).append(s)

    if not by_vm:
        return result_map

    def _check_vm(vm_name: str) -> dict[str, SessionStatus]:
        return batch_check_status(by_vm[vm_name], target=vm_targets[vm_name])

    tasks = {vm_name: partial(_check_vm, vm_name) for vm_name in by_vm}
    with cancelling_futures(tasks) as futures:
        for future in as_completed(futures):
            try:
                result_map.update(future.result())
            except UserAbort:
                raise
            except AgentworksError:
                continue

    return result_map
