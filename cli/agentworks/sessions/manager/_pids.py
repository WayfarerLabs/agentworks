"""PID recovery and session-target selection for sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import PID_STOPPED, SessionMode
from agentworks.errors import (
    ConnectivityError,
    NotFoundError,
    StateError,
)

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, VMRow
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

    Single-session paths use this to make stop / restart operations
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
    announce: bool = True,
    persist: bool = True,
) -> bool:
    """Resolve a single session's runtime identity, optionally persisting it.

    Raises StateError if the session is alive but PID/boot_id can't be recovered,
    or ConnectivityError if the VM is unreachable.
    """
    from agentworks.sessions.tmux import ProbeStatus, capture_tmux_server_fingerprint, probe_tmux_session

    sock = session.socket_path
    if sock is None:
        presence = probe_tmux_session(
            session.name,
            run_command=target.run,
            socket_path=None,
        )
        if presence is ProbeStatus.PRESENT:
            raise StateError(
                f"session '{session.name}' is live on a legacy shared tmux server",
                entity_kind="session",
                entity_name=session.name,
                hint=f"Run `agw session restart {session.name}` to migrate it.",
            )
        if presence is ProbeStatus.UNKNOWN:
            raise ConnectivityError(
                f"could not determine legacy runtime state for session '{session.name}'",
                entity_kind="session",
                entity_name=session.name,
            )
        if not _prove_stored_runtime_absent(session, target=target):
            raise StateError(
                f"session '{session.name}' may still own a legacy runtime",
                entity_kind="session",
                entity_name=session.name,
            )
    else:
        sudo = session.mode == SessionMode.AGENT.value
        probe = capture_tmux_server_fingerprint(target=target, socket_path=sock, sudo=sudo)
        if probe.status is ProbeStatus.PRESENT:
            fingerprint = probe.fingerprint
            assert fingerprint is not None
            observed_boot_id = _validated_observed_boot_id(fingerprint.boot_id, session=session)
            stored_boot_id = _validated_stored_boot_id(session) if session.boot_id is not None else None
            stored_ticks = _validated_stored_start_ticks(session)
            if (
                (session.pid is None or session.pid == fingerprint.pid)
                and (stored_boot_id is None or stored_boot_id == observed_boot_id)
                and (stored_ticks is None or stored_ticks == fingerprint.start_ticks)
            ):
                _apply_repaired_runtime(
                    session,
                    db=db,
                    persist=persist,
                    socket_path=sock,
                    pid=fingerprint.pid,
                    boot_id=observed_boot_id,
                    tmux_server_start_ticks=fingerprint.start_ticks,
                )
                if announce:
                    output.info(f"Recovered runtime identity for session '{session.name}'")
                return True
            raise StateError(
                f"session '{session.name}' has a live tmux server whose identity does not match its row",
                entity_kind="session",
                entity_name=session.name,
                hint="Investigate the tmux server manually.",
            )
        if probe.status is ProbeStatus.UNKNOWN:
            raise StateError(
                f"session '{session.name}' runtime state could not be determined",
                entity_kind="session",
                entity_name=session.name,
                hint="Investigate the tmux server manually.",
            )
        if not _prove_stored_runtime_absent(session, target=target, sudo=sudo):
            raise StateError(
                f"session '{session.name}' runtime absence could not be proved",
                entity_kind="session",
                entity_name=session.name,
                hint="Investigate the tmux server manually.",
            )

    _apply_repaired_runtime(
        session,
        db=db,
        persist=persist,
        socket_path=sock,
        pid=PID_STOPPED,
        boot_id=None,
        tmux_server_start_ticks=None,
    )
    if announce:
        output.info(f"Session '{session.name}' is not running; marked stopped")
    return True


def _apply_repaired_runtime(
    session: SessionRow,
    *,
    db: Database,
    persist: bool,
    socket_path: str | None,
    pid: int | None,
    boot_id: str | None,
    tmux_server_start_ticks: int | None,
) -> None:
    """Apply an observed repair in memory and optionally persist it."""
    if persist:
        db.update_session_runtime(
            session.name,
            socket_path=socket_path,
            pid=pid,
            boot_id=boot_id,
            tmux_server_start_ticks=tmux_server_start_ticks,
        )
    session.socket_path = socket_path
    session.pid = pid
    session.boot_id = boot_id
    session.tmux_server_start_ticks = tmux_server_start_ticks


def _validated_stored_start_ticks(session: SessionRow) -> int | None:
    """Return a valid stored process start time or fail closed."""
    value = session.tmux_server_start_ticks
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StateError(
            f"session '{session.name}' has an invalid stored tmux process start time",
            entity_kind="session",
            entity_name=session.name,
            hint="Repair the persisted runtime identity before retrying.",
        )
    return value


def _validated_stored_boot_id(session: SessionRow) -> str:
    """Return a canonical persisted boot UUID or fail closed."""
    from agentworks.sessions.tmux import canonical_boot_id

    value = canonical_boot_id(session.boot_id)
    if value is None:
        raise StateError(
            f"session '{session.name}' has an invalid stored VM boot identity",
            entity_kind="session",
            entity_name=session.name,
            hint="Repair the persisted runtime identity before retrying.",
        )
    return value


def _validated_observed_boot_id(value: object, *, session: SessionRow) -> str:
    """Return a canonical observed boot UUID or fail closed."""
    from agentworks.sessions.tmux import canonical_boot_id

    boot_id = canonical_boot_id(value)
    if boot_id is None:
        raise StateError(
            f"session '{session.name}' has an invalid observed VM boot identity",
            entity_kind="session",
            entity_name=session.name,
        )
    return boot_id


def _prove_stored_runtime_absent(
    session: SessionRow,
    *,
    target: Transport,
    sudo: bool = False,
) -> bool:
    """Prove the stored process incarnation absent without signaling its PID."""
    from agentworks.sessions.tmux import ProbeStatus, probe_process_start_ticks

    if session.pid is None or session.pid <= 0 or session.boot_id is None:
        raise StateError(
            f"session '{session.name}' lacks the stored identity required to prove runtime absence",
            entity_kind="session",
            entity_name=session.name,
        )
    stored_boot_id = _validated_stored_boot_id(session)
    stored_ticks = _validated_stored_start_ticks(session)
    current_boot = _mgr._get_boot_id(target)
    if current_boot is None:
        raise ConnectivityError(
            f"could not read the VM boot identity for session '{session.name}'",
            entity_kind="session",
            entity_name=session.name,
        )
    if current_boot != stored_boot_id:
        return True

    pid_presence = _mgr._pid_presence(session.pid, target=target, sudo=sudo)
    if pid_presence is ProbeStatus.ABSENT:
        return True
    if pid_presence is ProbeStatus.UNKNOWN:
        raise ConnectivityError(
            f"could not determine whether the stored process for session '{session.name}' exists",
            entity_kind="session",
            entity_name=session.name,
        )
    if stored_ticks is None:
        return False

    ticks = probe_process_start_ticks(session.pid, target=target, sudo=sudo)
    if ticks.status is ProbeStatus.ABSENT:
        return _mgr._pid_presence(session.pid, target=target, sudo=sudo) is ProbeStatus.ABSENT
    if ticks.status is ProbeStatus.UNKNOWN or ticks.value is None:
        raise StateError(
            f"could not verify the stored process start time for session '{session.name}'",
            entity_kind="session",
            entity_name=session.name,
        )
    return ticks.value != stored_ticks


def _needs_repair(session: SessionRow) -> bool:
    """True if the session is missing PID or boot_id and needs auto-repair."""
    if session.pid == PID_STOPPED:
        return False
    return session.pid is None or session.boot_id is None


def _ensure_pid(
    session: SessionRow,
    *,
    target: Transport,
    db: Database,
    persist: bool = True,
) -> SessionRow:
    """Auto-recover PID + boot ID for a session missing either.

    Strict gate: after this returns, the session is guaranteed to be either
    PID_STOPPED or have valid PID + boot_id. Raises StateError if the
    session cannot be resolved.
    """
    if not _needs_repair(session):
        return session
    _repair_session_pid(session, target=target, db=db, announce=persist, persist=persist)  # raises on failure
    if not persist:
        return session
    result = db.get_session(session.name)
    assert result is not None
    return result


def ensure_pids_batch(
    sessions: list[SessionRow],
    *,
    db: Database,
    config: Config,
    announce: bool = True,
    persist: bool = True,
) -> list[SessionRow]:
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
                from agentworks.vms.manager import require_vm_ssh_boundary

                require_vm_ssh_boundary(db, config, vm)
                vm_cache[ws.vm_name] = _mgr.transport(vm, config)
            except Exception as exc:
                if announce:
                    output.warn(f"Cannot reach VM '{ws.vm_name}': {exc}")
                continue
        by_vm.setdefault(ws.vm_name, []).append(s)

    repaired_names: set[str] = set()
    for vm_name, vm_sessions in by_vm.items():
        target = vm_cache[vm_name]
        for session in vm_sessions:
            try:
                if _repair_session_pid(session, target=target, db=db, announce=announce, persist=persist):
                    repaired_names.add(session.name)
            except (ConnectivityError, StateError) as exc:
                if announce:
                    output.warn(str(exc))
            except Exception as exc:
                if announce:
                    output.warn(f"Failed to repair session '{session.name}': {exc}")

    # Return original list with repaired sessions refreshed from DB
    if not repaired_names or not persist:
        return sessions
    result = []
    for s in sessions:
        if s.name in repaired_names:
            refreshed = db.get_session(s.name)
            result.append(refreshed if refreshed else s)
        else:
            result.append(s)
    return result
