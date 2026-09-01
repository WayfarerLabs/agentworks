"""Session liveness checks (single and batched)."""

from __future__ import annotations

import shlex
from types import SimpleNamespace
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import PID_STOPPED, SessionStatus
from agentworks.errors import (
    AgentworksError,
    StateError,
)
from agentworks.sessions.tmux import ProbeStatus, probe_tmux_server, probe_tmux_session

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.transports import Transport


def _pid_presence(pid: int, *, target: Transport) -> ProbeStatus:
    """Probe a PID without treating a transport failure as process absence."""
    from agentworks.sessions.tmux import _test_presence_from_result

    return _test_presence_from_result(target.run(f"test -d /proc/{pid}", check=False))


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
    target: Transport,
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
    if session.pid == PID_STOPPED:
        return SessionStatus.STOPPED
    if session.pid is None or session.boot_id is None:
        return SessionStatus.UNKNOWN

    if session.socket_path is not None:
        return _check_dedicated_session(session, target=target)
    # Legacy admin session predating per-session sockets. Surface as a
    # typed StateError so the CLI's top-level error wrapper renders it
    # as a one-liner; the new admin-mode path always stores a
    # socket_path.
    raise StateError(
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
        return SessionStatus.OK

    server_presence = probe_tmux_server(run_command=target.run, socket_path=socket_path)
    if server_presence is ProbeStatus.UNKNOWN:
        return SessionStatus.UNKNOWN
    if server_presence is ProbeStatus.PRESENT:
        return SessionStatus.RESIDUAL

    # has-session failed -- STOPPED or BROKEN?
    assert session.pid is not None and session.pid > 0
    current_boot = _mgr._get_boot_id(target)
    if current_boot is None:
        return SessionStatus.UNKNOWN  # can't verify boot cycle, unsafe to offer --force
    stored_boot_id = _mgr._validated_stored_boot_id(session)
    if stored_boot_id != current_boot:
        return SessionStatus.STOPPED  # stale boot, PID is meaningless
    pid_presence = _pid_presence(session.pid, target=target)
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

    Returns {session_name: SessionStatus}. Sessions with pid=None or PID_STOPPED
    are excluded (callers handle those via the enum directly).
    """
    from agentworks.sessions.tmux import canonical_boot_id, tmux_cmd

    checkable = [s for s in sessions if s.pid is not None and s.pid > 0 and s.boot_id is not None]
    if not checkable:
        return {}

    # Preserve one SSH round-trip while returning only hex-encoded probe facts.
    # Python owns the tmux diagnostic vocabulary; the remote shell captures
    # facts without interpreting or exposing third-party text.
    encoder = "hex() { od -An -tx1 | tr -d ' \\n'; }; "
    parts = []
    for s in checkable:
        if s.socket_path is None:
            continue
        q_session = shlex.quote(f"={s.name}")  # exact tmux target
        name = s.name  # raw for output field (names are validated, no shell-special chars)
        has_cmd = tmux_cmd(f"has-session -t {q_session}", s.socket_path)
        server_cmd = tmux_cmd("list-sessions", s.socket_path)
        parts.append(
            f"HERR=$({has_cmd} 2>&1 >/dev/null); H=$?; "
            'HHEX=$(printf %s "$HERR" | hex); '
            f"SERR=$({server_cmd} 2>&1 >/dev/null); S=$?; "
            'SHEX=$(printf %s "$SERR" | hex); '
            f"BOOT=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null); B=$?; "
            'BOOT_HEX=$(printf %s "$BOOT" | hex); '
            f"test -d /proc/{s.pid}; P=$?; "
            f'printf "S:{name}:%s:%s:%s:%s:%s:%s:%s\\n" "$H" "$HHEX" "$S" "$SHEX" "$B" "$BOOT_HEX" "$P"'
        )
    if not parts:
        return {}
    cmd = encoder + "; ".join(parts)

    result = target.run(cmd, check=False)
    stdout = getattr(result, "stdout", "") or ""

    status_map: dict[str, SessionStatus] = {
        session.name: SessionStatus.UNKNOWN for session in checkable if session.socket_path is not None
    }
    sessions_by_name = {session.name: session for session in checkable}

    if result.returncode not in {0, 1}:
        return status_map

    for line in stdout.strip().splitlines():
        if not line.startswith("S:"):
            continue
        fields = line.split(":")
        if len(fields) != 9:
            continue
        name = fields[1]
        session = sessions_by_name.get(name)
        if session is None or name not in status_map:
            continue

        has_presence = _batch_tmux_presence(fields[2], fields[3], missing_target_is_absent=True)
        if has_presence is ProbeStatus.PRESENT:
            status_map[name] = SessionStatus.OK
            continue
        if has_presence is ProbeStatus.UNKNOWN:
            continue

        server_presence = _batch_tmux_presence(fields[4], fields[5], missing_target_is_absent=False)
        if server_presence is ProbeStatus.PRESENT:
            status_map[name] = SessionStatus.RESIDUAL
            continue
        if server_presence is ProbeStatus.UNKNOWN:
            continue

        try:
            boot_returncode = int(fields[6])
            observed_boot = bytes.fromhex(fields[7]).decode("ascii")
            pid_returncode = int(fields[8])
            stored_boot = _mgr._validated_stored_boot_id(session)
        except (StateError, UnicodeDecodeError, ValueError):
            continue
        current_boot = canonical_boot_id(observed_boot) if boot_returncode == 0 else None
        if current_boot is None or pid_returncode not in {0, 1}:
            continue
        if stored_boot != current_boot or pid_returncode == 1:
            status_map[name] = SessionStatus.STOPPED
        else:
            status_map[name] = SessionStatus.BROKEN

    return status_map


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


def batch_check_all_sessions(
    sessions: list[SessionRow],
    *,
    db: Database,
    config: Config,
) -> dict[str, SessionStatus]:
    """Batch status check grouped by VM, parallel across VMs (capped at 8).

    Returns {session_name: SessionStatus}. Sessions with no reachable VM or
    pid=None/PID_STOPPED are excluded from the result.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from contextvars import copy_context

    # Resolve each session's VM and group
    by_vm: dict[str, list[SessionRow]] = {}
    vm_targets: dict[str, Transport] = {}

    for s in sessions:
        ws = db.get_workspace(s.workspace_name)
        if not ws:
            continue
        if ws.vm_name not in vm_targets:
            vm = db.get_vm(ws.vm_name)
            if not vm or not vm.tailscale_host:
                continue
            try:
                from agentworks.vms.manager import require_vm_ssh_boundary

                require_vm_ssh_boundary(db, config, vm)
                vm_targets[ws.vm_name] = _mgr.transport(vm, config)
            except AgentworksError:
                continue
        by_vm.setdefault(ws.vm_name, []).append(s)

    if not by_vm:
        return {}

    result_map: dict[str, SessionStatus] = {}

    def _check_vm(vm_name: str) -> dict[str, SessionStatus]:
        return batch_check_status(by_vm[vm_name], target=vm_targets[vm_name])

    with ThreadPoolExecutor(max_workers=min(8, len(by_vm))) as executor:
        futures = {executor.submit(copy_context().run, _check_vm, name): name for name in by_vm}
        for future in as_completed(futures):
            vm_name = futures[future]
            try:
                result_map.update(future.result())
            except Exception as exc:
                output.warn(f"Failed to check sessions on VM '{vm_name}': {exc}")

    return result_map
