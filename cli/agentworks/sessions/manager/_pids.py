"""PID recovery and session-target selection for sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import PID_STOPPED, DatabaseDriverError, SessionMode, SessionRuntimeSnapshot
from agentworks.errors import (
    ConnectivityError,
    NotFoundError,
    StateConflictError,
    StateError,
    UserAbort,
)

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, SessionRow, VMRow
    from agentworks.execution.access import VMAccess
    from agentworks.sessions.tmux import RunCommand
    from agentworks.vms.initializer.phase_a import OperationLog


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


def _session_agent(db: Database, session: SessionRow) -> AgentRow | None:
    """The agent that owns ``session``, or ``None`` for an admin-mode session.

    Raises ``NotFoundError`` when an agent-mode session names no agent, or an
    agent that is gone.
    """
    if session.mode == SessionMode.ADMIN.value:
        return None
    agent = db.get_agent(session.agent_name) if session.agent_name is not None else None
    if agent is None:
        raise NotFoundError(
            f"agent '{session.agent_name}' (referenced by session '{session.name}') not found",
            entity_kind="agent",
            entity_name=session.agent_name,
        )
    return agent


def _identity_access(vm: VMRow, config: Config, agent: AgentRow | None, *, logger: OperationLog | None) -> VMAccess:
    """The admin's ``VMAccess``, or ``agent``'s, logging to the operation's own ``logger``.

    ``logger`` is ``None`` where the operation keeps no log: a status read, or
    a preflight that runs before secret resolution, whose operation opens its
    log once the redactions are known. Building the access connects to
    nothing.
    """
    import agentworks.execution as execution
    from agentworks.execution.access import Admin, Agent

    identity = Admin() if agent is None else Agent(agent.name, agent.linux_user)
    return execution.vm_access(vm, config, identity, log=None if logger is None else logger.record)


def _session_access(
    session: SessionRow, *, vm: VMRow, config: Config, db: Database, logger: OperationLog | None
) -> VMAccess:
    """The ``VMAccess`` of the session's owning identity: the admin for an admin-mode session, else its agent.

    Building it connects to nothing; ``_verified_session_access`` is the one
    that proves an agent's direct SSH works first.
    """
    return _identity_access(vm, config, _session_agent(db, session), logger=logger)


def _verified_session_access(
    session: SessionRow,
    *,
    vm: VMRow,
    config: Config,
    db: Database,
    admin: VMAccess,
    logger: OperationLog | None,
) -> VMAccess:
    """The session owner's access, for destructive operations on a single session, after the I19 probe.

    For an admin session that is ``admin`` itself. For an agent session it is
    the agent's access, logging to ``logger``, once a probe proves direct SSH
    as the agent works (``_assert_agent_access_works``): an agent that
    predates the direct-SSH rollout raises a ``StateError`` with the reinit
    hint before anything changes. That probe connects, so a caller that only
    reads, such as a status listing, takes ``_session_access`` instead.
    """
    agent = _session_agent(db, session)
    if agent is None:
        return admin
    from agentworks.agents.manager.access import _assert_agent_access_works

    access = _identity_access(vm, config, agent, logger=logger)
    _assert_agent_access_works(access, admin, agent)
    return access


def _repair_session_pid(
    session: SessionRow,
    *,
    admin: VMAccess,
    db: Database,
    announce: bool = True,
) -> bool:
    """Core repair logic for a single session. Returns True if the DB was updated.

    Every read runs on ``admin`` as one dispatch within the observation
    deadline, a dedicated server's as its owner after its socket path passes
    the owner rule (``resolve_session_tmux_server``, ``owner_run_command``). Raises
    StateError if the session is alive but PID/boot_id can't be recovered,
    or ConnectivityError if the VM is unreachable.
    """
    from agentworks.sessions.tmux import (
        ProbeStatus,
        _matches_launch_attempt,
        access_run_command,
        admin_socket_path,
        capture_tmux_server_fingerprint,
        probe_tmux_session,
        require_socket_path,
    )
    from agentworks.status_observation import GUEST_OBSERVATION_TIMEOUT_SECONDS

    expected = SessionRuntimeSnapshot.from_session(session)
    server = _mgr.resolve_session_tmux_server(db, session)
    if server is None:
        # A legacy row's shared server, and the dedicated address it may take, are the admin's.
        run_command: RunCommand = access_run_command(admin, deadline=GUEST_OBSERVATION_TIMEOUT_SECONDS, elevated=False)
        presence = probe_tmux_session(
            session.name,
            run_command=run_command,
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
        # Older failed admin launches retained no address. Only known absence
        # permits assigning the dedicated address; a live legacy row keeps its
        # existing migration path.
        if session.mode == SessionMode.ADMIN.value and session.pid in {None, PID_STOPPED}:
            workspace = db.get_workspace(session.workspace_name)
            vm = db.get_vm(workspace.vm_name) if workspace is not None else None
            assert vm is not None
            sock = admin_socket_path(vm.admin_username, session.name)
            require_socket_path(sock, run_command=run_command)
            dedicated = capture_tmux_server_fingerprint(run_command=run_command, socket_path=sock)
            if dedicated.status is not ProbeStatus.ABSENT:
                raise StateError(
                    f"session '{session.name}' dedicated runtime absence could not be proved",
                    entity_kind="session",
                    entity_name=session.name,
                )
            if session.pid != PID_STOPPED and session.boot_id is None:
                boot_id = _mgr._get_boot_id(run_command)
                if boot_id is not None:
                    db.compare_and_set_session_runtime_repair(
                        expected, socket_path=None, pid=None, boot_id=boot_id, tmux_server_start_ticks=None
                    )
                raise StateError(
                    f"session '{session.name}' has no recorded launch identity",
                    entity_kind="session",
                    entity_name=session.name,
                    hint="Stop and start its VM, then retry to prove the old workload is gone.",
                )
        if session.pid != PID_STOPPED and not _prove_stored_runtime_absent(session, run_command=run_command):
            raise StateError(
                f"session '{session.name}' may still own a legacy runtime",
                entity_kind="session",
                entity_name=session.name,
            )
    else:
        sock = server.socket_path
        run_command = _mgr.owner_run_command(admin, server)
        require_socket_path(sock, run_command=run_command)
        probe = capture_tmux_server_fingerprint(run_command=run_command, socket_path=sock)
        if probe.status is ProbeStatus.PRESENT:
            fingerprint = probe.fingerprint
            assert fingerprint is not None
            observed_boot_id = _validated_observed_boot_id(fingerprint.boot_id, session=session)
            stored_boot_id = _validated_stored_boot_id(session) if session.boot_id is not None else None
            stored_ticks = _validated_stored_start_ticks(session)
            if (
                session.pid != PID_STOPPED
                and (session.pid is None or session.pid == fingerprint.pid)
                and (stored_boot_id is None or stored_boot_id == observed_boot_id)
                and (stored_ticks is None or stored_ticks == fingerprint.start_ticks)
                and (
                    (session.pid is not None and stored_boot_id is not None and stored_ticks is not None)
                    or (
                        stored_boot_id == observed_boot_id
                        and _matches_launch_attempt(
                            run_command=run_command,
                            socket_path=sock,
                            session_name=session.name,
                            session_uuid=session.session_uuid,
                            run_id=session.run_id,
                            fingerprint=fingerprint,
                        )
                    )
                )
            ):
                db.compare_and_set_session_runtime_repair(
                    expected,
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
                hint=(
                    "Its stored runtime identity is incomplete. Stop the native workload manually, or stop and start "
                    "its VM, then retry."
                    if session.pid is not None
                    and session.pid > 0
                    and stored_boot_id is not None
                    and stored_ticks is None
                    else "Investigate the tmux server manually."
                ),
            )
        if probe.status is ProbeStatus.UNKNOWN:
            raise StateError(
                f"session '{session.name}' runtime state could not be determined",
                entity_kind="session",
                entity_name=session.name,
                hint="Investigate the tmux server manually.",
            )
        if session.boot_id is None:
            boot_id = _mgr._get_boot_id(run_command)
            if boot_id is not None:
                db.compare_and_set_session_runtime_repair(
                    expected,
                    socket_path=sock,
                    pid=None,
                    boot_id=boot_id,
                    tmux_server_start_ticks=None,
                )
            raise StateError(
                f"session '{session.name}' has no recorded launch identity",
                entity_kind="session",
                entity_name=session.name,
                hint="Stop and start its VM, then retry this operation to prove the old workload is gone.",
            )
        if not _prove_stored_runtime_absent(session, run_command=run_command):
            raise StateError(
                f"session '{session.name}' runtime absence could not be proved",
                entity_kind="session",
                entity_name=session.name,
                hint="Investigate the tmux server manually.",
            )

    db.compare_and_set_session_runtime_repair(
        expected,
        socket_path=sock,
        pid=PID_STOPPED,
        boot_id=None,
        tmux_server_start_ticks=None,
    )
    if announce:
        output.info(f"Session '{session.name}' is not running; marked stopped")
    return True


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


def _prove_stored_runtime_absent(session: SessionRow, *, run_command: RunCommand) -> bool:
    """Prove the stored process incarnation absent without signaling its PID, reading through ``run_command``."""
    if session.boot_id is None:
        raise StateError(
            f"session '{session.name}' lacks the stored identity required to prove runtime absence",
            entity_kind="session",
            entity_name=session.name,
        )
    stored_boot_id = _validated_stored_boot_id(session)
    stored_ticks = _validated_stored_start_ticks(session)
    return _prove_runtime_absent(
        session_name=session.name,
        stored_pid=session.pid,
        stored_boot_id=stored_boot_id,
        stored_start_ticks=stored_ticks,
        run_command=run_command,
    )


def _prove_runtime_absent(
    *,
    session_name: str,
    stored_pid: int | None,
    stored_boot_id: str,
    stored_start_ticks: int | None,
    run_command: RunCommand,
) -> bool:
    """Prove one validated process incarnation absent from primitive fields."""
    from agentworks.sessions.tmux import ProbeStatus, probe_process_start_ticks

    current_boot = _mgr._get_boot_id(run_command)
    if current_boot is None:
        raise ConnectivityError(
            f"could not read the VM boot identity for session '{session_name}'",
            entity_kind="session",
            entity_name=session_name,
        )
    if current_boot != stored_boot_id:
        return True

    if stored_pid is None or stored_pid <= 0:
        raise StateError(
            f"session '{session_name}' has an uncertain launch from the current VM boot",
            entity_kind="session",
            entity_name=session_name,
            hint="Stop and start its VM, then retry this operation to prove the old workload is gone.",
        )

    pid_presence = _mgr._pid_presence(stored_pid, run_command=run_command)
    if pid_presence is ProbeStatus.ABSENT:
        return True
    if pid_presence is ProbeStatus.UNKNOWN:
        raise ConnectivityError(
            f"could not determine whether the stored process for session '{session_name}' exists",
            entity_kind="session",
            entity_name=session_name,
        )
    if stored_start_ticks is None:
        return False

    ticks = probe_process_start_ticks(stored_pid, run_command=run_command)
    if ticks.status is ProbeStatus.ABSENT:
        return _mgr._pid_presence(stored_pid, run_command=run_command) is ProbeStatus.ABSENT
    if ticks.status is ProbeStatus.UNKNOWN or ticks.value is None:
        # The process may exit between the presence probe and the stat read.
        # Require an explicit absence fact rather than treating unreadable stat as absence.
        if _mgr._pid_presence(stored_pid, run_command=run_command) is ProbeStatus.ABSENT:
            return True
        raise StateError(
            f"could not verify the stored process start time for session '{session_name}'",
            entity_kind="session",
            entity_name=session_name,
        )
    return ticks.value != stored_start_ticks


def _needs_repair(session: SessionRow) -> bool:
    """True if the session is missing PID or boot_id and needs auto-repair."""
    if session.pid == PID_STOPPED:
        return False
    return session.pid is None or session.boot_id is None


def _ensure_pid(session: SessionRow, *, admin: VMAccess, db: Database) -> SessionRow:
    """Auto-recover PID + boot ID for a session missing either.

    Strict gate: after this returns, the session is guaranteed to be either
    PID_STOPPED or have valid PID + boot_id. Raises StateError if the
    session cannot be resolved.
    """
    if not _needs_repair(session) and session.socket_path is not None:
        return session
    if session.socket_path is None and session.pid is not None and session.pid > 0:
        return session
    _repair_session_pid(session, admin=admin, db=db)  # raises on failure
    result = db.get_session(session.name)
    assert result is not None
    return result


def ensure_pids_batch(
    sessions: list[SessionRow],
    *,
    db: Database,
    config: Config,
    announce: bool = True,
) -> list[SessionRow]:
    """Auto-recover PID + boot ID for sessions missing either. Returns updated list."""
    need_repair = [s for s in sessions if _needs_repair(s)]
    if not need_repair:
        return sessions

    # Group by VM (not workspace) to reuse one admin access per VM
    by_vm: dict[str, list[SessionRow]] = {}
    vm_cache: dict[str, VMAccess] = {}
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
                vm_cache[ws.vm_name] = _identity_access(vm, config, None, logger=None)
            except UserAbort:
                raise
            except Exception as exc:
                if announce:
                    output.warn(f"Cannot reach VM '{ws.vm_name}': {exc}")
                continue
        by_vm.setdefault(ws.vm_name, []).append(s)

    repaired_names: set[str] = set()
    for vm_name, vm_sessions in by_vm.items():
        admin = vm_cache[vm_name]
        for session in vm_sessions:
            try:
                if _repair_session_pid(session, admin=admin, db=db, announce=announce):
                    repaired_names.add(session.name)
            except (StateConflictError, DatabaseDriverError):
                raise
            except (ConnectivityError, StateError) as exc:
                if announce:
                    output.warn(str(exc))
            except UserAbort:
                raise
            except Exception as exc:
                if announce:
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
