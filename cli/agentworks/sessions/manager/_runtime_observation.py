"""Read-only runtime classification preserves the existing absence proofs.

Every read runs on the admin's access as one dispatch within the observation
deadline, a dedicated server's as that server's owner (``owner_run_command``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks.db import PID_STOPPED, SessionStatus
from agentworks.errors import StateError
from agentworks.sessions.manager import _teardown
from agentworks.sessions.manager._teardown import ManagedSocket
from agentworks.sessions.tmux import ProbeStatus, access_run_command
from agentworks.status_observation import GUEST_OBSERVATION_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from typing import NoReturn

    from agentworks.db import Database, SessionRow, VMRow
    from agentworks.execution.access import VMAccess


def observe_session_runtime(
    session: SessionRow,
    *,
    admin: VMAccess,
    db: Database,
) -> SessionStatus:
    """Classify exact presence and stored boot/process absence without evidence writes.

    A legacy row's shared server, the admin's default one, is probed on ``admin``.
    """
    from agentworks.sessions.tmux import capture_tmux_server_fingerprint, probe_tmux_session, require_socket_path

    if session.socket_path is not None:
        status = _mgr.check_session_status(session, admin=admin, db=db)
        if status is SessionStatus.UNKNOWN and _mgr._needs_repair(session) and session.boot_id is not None:
            socket = _teardown._validated_socket(db, session)
            run_command = _mgr.owner_run_command(admin, session, socket)
            require_socket_path(socket.path, run_command=run_command)
            probe = capture_tmux_server_fingerprint(run_command=run_command, socket_path=socket.path)
            if probe.status is ProbeStatus.ABSENT and _mgr._prove_stored_runtime_absent(
                session, run_command=run_command
            ):
                return SessionStatus.STOPPED
        return status
    # A legacy row is always an admin session: the schema requires an agent
    # session's socket (CHECK in db/migrations.py:163), so the shared server
    # it names is the admin's own, and these reads run unelevated.
    run_command = access_run_command(admin, deadline=GUEST_OBSERVATION_TIMEOUT_SECONDS, elevated=False)
    presence = probe_tmux_session(session.name, run_command=run_command, socket_path=None)
    if presence is ProbeStatus.PRESENT and session.pid in {None, PID_STOPPED}:
        return SessionStatus.UNKNOWN
    if (
        presence is ProbeStatus.ABSENT
        and session.pid is None
        and not _mgr._prove_stored_runtime_absent(session, run_command=run_command)
    ):
        return SessionStatus.UNKNOWN
    return {
        ProbeStatus.PRESENT: SessionStatus.RUNNING,
        ProbeStatus.ABSENT: SessionStatus.STOPPED,
        ProbeStatus.UNKNOWN: SessionStatus.UNKNOWN,
    }[presence]


def require_unaddressed_session_absence(session: SessionRow, *, vm: VMRow, admin: VMAccess) -> None:
    """An old failed admin row may use its canonical address only after native absence."""
    if session.socket_path is not None or session.pid not in {None, PID_STOPPED}:
        return
    from agentworks.sessions.tmux import admin_socket_path, capture_tmux_server_fingerprint, require_socket_path

    expected = admin_socket_path(vm.admin_username, session.name)
    run_command = _mgr.owner_run_command(admin, session, ManagedSocket(expected, vm.admin_username))
    require_socket_path(expected, run_command=run_command)
    probe = capture_tmux_server_fingerprint(run_command=run_command, socket_path=expected)
    if probe.status is not ProbeStatus.ABSENT:
        raise StateError(
            "session dedicated runtime absence could not be proved", entity_kind="session", entity_name=session.name
        )


def can_checkpoint_session(session: SessionRow, *, vm: VMRow, admin: VMAccess, db: Database) -> bool:
    """Recognize only the existing exact-absence, missing-boot checkpoint repair."""
    if session.boot_id is not None or session.pid == PID_STOPPED:
        return False
    from agentworks.sessions.tmux import capture_tmux_server_fingerprint, probe_tmux_session, require_socket_path

    if session.socket_path is None:
        if session.pid is not None:
            return False
        # A legacy row's shared server is the admin's own.
        run_command = access_run_command(admin, deadline=GUEST_OBSERVATION_TIMEOUT_SECONDS, elevated=False)
        if probe_tmux_session(session.name, run_command=run_command, socket_path=None) is not ProbeStatus.ABSENT:
            return False
        require_unaddressed_session_absence(session, vm=vm, admin=admin)
    else:
        socket = _teardown._validated_socket(db, session)
        run_command = _mgr.owner_run_command(admin, session, socket)
        require_socket_path(socket.path, run_command=run_command)
        if capture_tmux_server_fingerprint(run_command=run_command, socket_path=socket.path).status is not (
            ProbeStatus.ABSENT
        ):
            return False
    return _mgr._get_boot_id(run_command) is not None


def checkpoint_session_identity(db: Database, session: SessionRow, *, vm: VMRow, admin: VMAccess) -> NoReturn:
    """Re-prove absence, record the existing boot checkpoint, and always refuse launch."""
    if not can_checkpoint_session(session, vm=vm, admin=admin, db=db):
        raise StateError(
            "runtime identity checkpoint observation changed", entity_kind="session", entity_name=session.name
        )
    _mgr._ensure_pid(session, admin=admin, db=db)
    raise StateError(
        "runtime identity checkpoint requires a later VM boot before retry",
        entity_kind="session",
        entity_name=session.name,
    )
