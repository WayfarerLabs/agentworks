"""PID recovery and session-target selection for sessions."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import PID_STOPPED, SessionMode
from agentworks.errors import (
    ConnectivityError,
    NotFoundError,
    StateError,
)
from agentworks.ssh import SSH_TRANSPORT_ERROR

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, VMRow
    from agentworks.sessions.tmux import RunCommand
    from agentworks.transports import Transport


def _resolve_session_linux_user(db: Database, session: SessionRow, vm: VMRow) -> str:
    """Resolve the Linux user for a session.

    Agent-mode sessions look up the agent by name. Admin-mode sessions use the VM admin.
    """
    if session.agent_name:
        agent = db.get_agent(session.agent_name)
        if agent is None:
            raise NotFoundError(
                f"agent '{session.agent_name}' not found (referenced by session '{session.name}')",
                entity_kind="agent",
                entity_name=session.agent_name,
            )
        return agent.linux_user
    return vm.admin_username


def _kill_session(
    session_name: str,
    *,
    run_command: RunCommand,
    socket_path: str | None,
) -> bool:
    """Kill a session on its expected tmux server. Returns True if successful."""
    from agentworks.sessions.tmux import kill_session

    return kill_session(session_name, run_command=run_command, socket_path=socket_path)


def _build_session_target(
    session: SessionRow,
    *,
    vm: VMRow,
    config: Config,
    db: Database,
    admin_target: Transport,
) -> Transport:
    """Pick the SSH transport for destructive operations on a single session.

    Returns a ``Transport`` whose SSH user is the session's owning Linux user
    (admin for admin-mode, agent for agent-mode). For agent sessions, builds
    an agent ``Transport`` and probes it; raises StateError with a reinit hint
    if the agent's authorized_keys aren't provisioned.
    For admin sessions, returns the admin target unchanged.

    Single-session paths use this to make kill / restart operations
    consistent with create: every destructive step on an agent session
    goes via direct agent SSH. Because the returned target always owns
    the session it will operate on, callers can issue destructive commands
    without sudo. Batch paths intentionally don't use this helper; they
    keep admin's target across all sessions and pass ``sudo=True`` to
    reach into agent tmux servers (carve-out for batch ops).
    """
    if session.mode == SessionMode.ADMIN.value:
        return admin_target

    if session.agent_name is None:
        raise NotFoundError(
            f"session '{session.name}' is agent-mode but has no agent_name",
            entity_kind="session",
            entity_name=session.name,
        )
    agent = db.get_agent(session.agent_name)
    if agent is None:
        raise NotFoundError(
            f"agent '{session.agent_name}' (referenced by session '{session.name}') not found",
            entity_kind="agent",
            entity_name=session.agent_name,
        )
    from agentworks.agents.manager import _assert_agent_ssh_works
    from agentworks.transports import agent_transport

    agent_target = agent_transport(vm, config, agent)
    _assert_agent_ssh_works(agent_target, agent)
    return agent_target


def _repair_session_pid(
    session: SessionRow,
    *,
    target: Transport,
    db: Database,
) -> bool:
    """Core repair logic for a single session. Returns True if the DB was updated.

    Raises StateError if the session is alive but PID/boot_id can't be recovered,
    or ConnectivityError if the VM is unreachable.
    """
    from agentworks.sessions.tmux import get_tmux_server_pid, tmux_cmd

    sock = session.socket_path
    q_session = shlex.quote(session.name)

    # Step 1: try has-session (the primary liveness check)
    has_cmd = tmux_cmd(f"has-session -t {q_session}", sock) + " 2>/dev/null"
    has_result = target.run(has_cmd, check=False)
    if has_result.returncode == SSH_TRANSPORT_ERROR:
        raise ConnectivityError(
            f"cannot reach VM for session '{session.name}' (SSH connection failed)",
            entity_kind="session",
            entity_name=session.name,
        )
    if has_result.ok:
        # Session is alive -- recover PID + boot ID
        pid = get_tmux_server_pid(target=target, socket_path=sock)
        boot_id = _mgr._get_boot_id(target) if pid is not None else None
        if pid is not None and boot_id is not None:
            db.update_session_pid(session.name, pid, boot_id=boot_id)
            output.warn(f"Recovered PID {pid} for session '{session.name}'")
            return True
        raise StateError(
            f"session '{session.name}' is alive but PID/boot ID recovery failed.",
            entity_kind="session",
            entity_name=session.name,
            hint="Investigate the tmux server manually.",
        )

    # Step 2: has-session failed -- determine if genuinely stopped or ambiguous
    if sock and target.run(f"test -e {shlex.quote(sock)}", sudo=True, check=False).ok:
        # Socket exists. Probe with sudo to distinguish stale from unreachable.
        probe_cmd = tmux_cmd("list-sessions", sock, sudo=True) + " 2>/dev/null"
        if target.run(probe_cmd, check=False).ok:
            raise StateError(
                f"session '{session.name}' has a live tmux server but it is unreachable.",
                entity_kind="session",
                entity_name=session.name,
                hint="This may indicate a permissions issue. Investigate manually.",
            )
        # Stale socket, server is dead
        db.update_session_pid(session.name, PID_STOPPED)
        output.warn(f"Session '{session.name}' is not running, marked stopped")
        return True

    # No socket (or admin session) and has-session failed -- genuinely stopped
    db.update_session_pid(session.name, PID_STOPPED)
    output.warn(f"Session '{session.name}' is not running, marked stopped")
    return True


def _needs_repair(session: SessionRow) -> bool:
    """True if the session is missing PID or boot_id and needs auto-repair."""
    if session.pid == PID_STOPPED:
        return False
    return session.pid is None or session.boot_id is None


def _ensure_pid(session: SessionRow, *, target: Transport, db: Database) -> SessionRow:
    """Auto-recover PID + boot ID for a session missing either.

    Strict gate: after this returns, the session is guaranteed to be either
    PID_STOPPED or have valid PID + boot_id. Raises StateError if the
    session cannot be resolved.
    """
    if not _needs_repair(session):
        return session
    _repair_session_pid(session, target=target, db=db)  # raises on failure
    result = db.get_session(session.name)
    assert result is not None
    return result


def ensure_pids_batch(sessions: list[SessionRow], *, db: Database, config: Config) -> list[SessionRow]:
    """Auto-recover PID + boot ID for sessions missing either. Returns updated list."""
    need_repair = [s for s in sessions if _needs_repair(s)]
    if not need_repair:
        return sessions

    # Group by VM (not workspace) to reuse one Transport per VM
    by_vm: dict[str, list[SessionRow]] = {}
    vm_cache: dict[str, Transport] = {}
    for s in need_repair:
        ws = db.get_workspace(s.workspace_name)
        if not ws:
            continue
        if ws.vm_name not in vm_cache:
            vm = db.get_vm(ws.vm_name)
            if not vm or not vm.tailscale_host:
                continue
            try:
                vm_cache[ws.vm_name] = _mgr.transport(vm, config)
            except Exception as exc:
                output.warn(f"Cannot reach VM '{ws.vm_name}': {exc}")
                continue
        by_vm.setdefault(ws.vm_name, []).append(s)

    repaired_names: set[str] = set()
    for vm_name, vm_sessions in by_vm.items():
        target = vm_cache[vm_name]
        for session in vm_sessions:
            try:
                if _repair_session_pid(session, target=target, db=db):
                    repaired_names.add(session.name)
            except (ConnectivityError, StateError) as exc:
                output.warn(str(exc))
            except Exception as exc:
                output.warn(f"Failed to repair session '{session.name}': {exc}")

    # Return original list with repaired sessions refreshed from DB
    if not repaired_names:
        return sessions
    result = []
    for s in sessions:
        if s.name in repaired_names:
            refreshed = db.get_session(s.name)
            result.append(refreshed if refreshed else s)
        else:
            result.append(s)
    return result
