"""Session liveness checks (single and batched)."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import PID_STOPPED, SessionStatus
from agentworks.errors import (
    StateError,
)
from agentworks.ssh import SSH_TRANSPORT_ERROR

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow
    from agentworks.transports import Transport


def _pid_alive(pid: int, *, target: Transport) -> bool:
    """Check if a PID is alive via /proc."""
    return target.run(f"test -d /proc/{pid}", check=False).ok


def _get_boot_id(target: Transport) -> str | None:
    """Read the current VM boot ID. Returns None on failure."""
    result = target.run("cat /proc/sys/kernel/random/boot_id", check=False)
    boot_id = (getattr(result, "stdout", "") or "").strip()
    return boot_id or None


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
    socket. Callers that aren't ``restart_session`` (attach, stop, etc.)
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
    from agentworks.sessions.tmux import tmux_cmd

    q_session = shlex.quote(session.name)
    cmd = tmux_cmd(f"has-session -t {q_session}", session.socket_path) + " 2>/dev/null"
    result = target.run(cmd, check=False)
    if result.returncode == SSH_TRANSPORT_ERROR:
        return SessionStatus.UNKNOWN  # SSH transport failure, not a session state
    if result.ok:
        return SessionStatus.OK

    # has-session failed -- STOPPED or BROKEN?
    assert session.pid is not None and session.pid > 0
    current_boot = _mgr._get_boot_id(target)
    if current_boot is None:
        return SessionStatus.UNKNOWN  # can't verify boot cycle, unsafe to offer --force
    if session.boot_id is not None and session.boot_id != current_boot:
        return SessionStatus.STOPPED  # stale boot, PID is meaningless
    if not _pid_alive(session.pid, target=target):
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
    from agentworks.sessions.tmux import tmux_cmd

    checkable = [s for s in sessions if s.pid is not None and s.pid > 0 and s.boot_id is not None]
    if not checkable:
        return {}

    # Build compound command: has-session with inline boot_id + PID for any
    # session whose has-session probe fails. Admin and agent sessions now
    # follow the same dedicated-socket model after the env-and-secrets SDD.
    # Legacy admin sessions with socket_path=None are skipped here with a
    # one-time warning so that `agw session list` against a VM with a mix of
    # legacy and new sessions still surfaces the new ones cleanly; the
    # operator-facing single-session paths (`session attach`, etc.) go
    # through `check_session_status`, which raises a typed StateError
    # pointing at `agw session restart` (the primitive that auto-migrates).
    legacy = [s.name for s in checkable if s.socket_path is None]
    if legacy:
        names = ", ".join(sorted(legacy))
        output.warn(
            f"{len(legacy)} session(s) predate the per-session-socket model; "
            f"`agw session restart` migrates them to the new shape: {names}"
        )

    parts = []
    for s in checkable:
        if s.socket_path is None:
            continue
        q_session = shlex.quote(s.name)  # quoted for tmux -t argument
        name = s.name  # raw for output field (names are validated, no shell-special chars)
        has_cmd = tmux_cmd(f"has-session -t {q_session}", s.socket_path)
        parts.append(
            f"{has_cmd} 2>/dev/null; "
            f"if [ $? -ne 0 ]; then "
            f"BOOT=$(cat /proc/sys/kernel/random/boot_id); "
            f"test -d /proc/{s.pid}; "
            f'echo "S:{name}:1:$BOOT:$?"; '
            f'else echo "S:{name}:0"; fi'
        )
    if not parts:
        return {}
    cmd = "; ".join(parts)

    result = target.run(cmd, check=False)
    stdout = getattr(result, "stdout", "") or ""

    status_map: dict[str, SessionStatus] = {}
    # Build a quick lookup for stored boot_ids
    boot_ids = {s.name: s.boot_id for s in checkable}

    for line in stdout.strip().splitlines():
        if not line.startswith("S:"):
            continue
        fields = line.split(":", maxsplit=4)
        if len(fields) < 3:
            continue
        name = fields[1]
        exit_code = fields[2]

        if exit_code == "0":
            status_map[name] = SessionStatus.OK
        elif len(fields) == 5:
            # Agent session failure: S:name:1:<boot_id>:<pid_exit>
            current_boot = fields[3]
            pid_exit = fields[4]
            if not current_boot:
                # Boot ID read failed -- can't safely determine STOPPED vs BROKEN
                pass  # omit from map, callers treat missing entries as unknown
            else:
                stored_boot = boot_ids.get(name)
                if stored_boot and stored_boot != current_boot:
                    status_map[name] = SessionStatus.STOPPED  # stale boot
                elif pid_exit == "0":
                    status_map[name] = SessionStatus.BROKEN  # PID alive, socket unreachable
                else:
                    status_map[name] = SessionStatus.STOPPED  # PID dead
        else:
            # Admin session failure
            status_map[name] = SessionStatus.STOPPED

    return status_map


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
            vm_targets[ws.vm_name] = _mgr.transport(vm, config)
        by_vm.setdefault(ws.vm_name, []).append(s)

    if not by_vm:
        return {}

    result_map: dict[str, SessionStatus] = {}

    def _check_vm(vm_name: str) -> dict[str, SessionStatus]:
        return batch_check_status(by_vm[vm_name], target=vm_targets[vm_name])

    with ThreadPoolExecutor(max_workers=min(8, len(by_vm))) as executor:
        futures = {executor.submit(_check_vm, name): name for name in by_vm}
        for future in as_completed(futures):
            vm_name = futures[future]
            try:
                result_map.update(future.result())
            except Exception as exc:
                output.warn(f"Failed to check sessions on VM '{vm_name}': {exc}")

    return result_map
