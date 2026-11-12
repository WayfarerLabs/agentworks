"""Read-only runtime classification preserves the existing absence proofs."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, cast

import agentworks.sessions.manager as _mgr
from agentworks.db import PID_STOPPED, SessionMode, SessionStatus
from agentworks.errors import StateError
from agentworks.sessions.tmux import ProbeStatus

if TYPE_CHECKING:
    from typing import NoReturn

    from agentworks.db import Database, SessionRow, VMRow
    from agentworks.transports import Transport


def _bounded(target: Transport) -> Transport:
    from ._status import _BoundedStatusTarget

    return cast("Transport", _BoundedStatusTarget(target))


def observe_session_runtime(
    session: SessionRow,
    *,
    admin_target: Transport,
    session_target: Transport | None = None,
) -> SessionStatus:
    """Classify exact presence and stored boot/process absence without evidence writes."""
    from agentworks.sessions.tmux import capture_tmux_server_fingerprint, probe_tmux_session, require_socket_path

    admin_target = _bounded(admin_target)
    session_target = _bounded(session_target) if session_target is not None else None
    sudo = session.mode == SessionMode.AGENT.value
    if session.socket_path is not None:
        status = _mgr.check_session_status(session, target=admin_target)
        if status is SessionStatus.UNKNOWN and _mgr._needs_repair(session) and session.boot_id is not None:
            require_socket_path(session.socket_path, run_command=partial(admin_target.run, sudo=sudo))
            probe = capture_tmux_server_fingerprint(target=admin_target, socket_path=session.socket_path, sudo=sudo)
            if probe.status is ProbeStatus.ABSENT and _mgr._prove_stored_runtime_absent(
                session, target=admin_target, sudo=sudo
            ):
                return SessionStatus.STOPPED
        return status
    target = session_target or admin_target
    run = target.run if session_target is not None else partial(target.run, sudo=sudo)
    presence = probe_tmux_session(session.name, run_command=run, socket_path=None)
    if presence is ProbeStatus.PRESENT and session.pid in {None, PID_STOPPED}:
        return SessionStatus.UNKNOWN
    if (
        presence is ProbeStatus.ABSENT
        and session.pid is None
        and not _mgr._prove_stored_runtime_absent(session, target=admin_target, sudo=sudo)
    ):
        return SessionStatus.UNKNOWN
    return {
        ProbeStatus.PRESENT: SessionStatus.RUNNING,
        ProbeStatus.ABSENT: SessionStatus.STOPPED,
        ProbeStatus.UNKNOWN: SessionStatus.UNKNOWN,
    }[presence]


def require_unaddressed_session_absence(session: SessionRow, *, vm: VMRow, admin_target: Transport) -> None:
    """An old failed admin row may use its canonical address only after native absence."""
    if session.socket_path is not None or session.pid not in {None, PID_STOPPED}:
        return
    from agentworks.sessions.tmux import admin_socket_path, capture_tmux_server_fingerprint, require_socket_path

    if session.mode != SessionMode.ADMIN.value:
        raise StateError("session has no managed socket identity", entity_kind="session", entity_name=session.name)
    admin_target = _bounded(admin_target)
    expected = admin_socket_path(vm.admin_username, session.name)
    require_socket_path(expected, run_command=admin_target.run)
    probe = capture_tmux_server_fingerprint(target=admin_target, socket_path=expected)
    if probe.status is not ProbeStatus.ABSENT:
        raise StateError(
            "session dedicated runtime absence could not be proved", entity_kind="session", entity_name=session.name
        )


def can_checkpoint_session(session: SessionRow, *, vm: VMRow, admin_target: Transport) -> bool:
    """Recognize only the existing exact-absence, missing-boot checkpoint repair."""
    if session.boot_id is not None or session.pid == PID_STOPPED:
        return False
    from agentworks.sessions.tmux import capture_tmux_server_fingerprint, probe_tmux_session, require_socket_path

    admin_target = _bounded(admin_target)
    sudo = session.mode == SessionMode.AGENT.value
    if session.socket_path is None:
        if session.pid is not None:
            return False
        if probe_tmux_session(session.name, run_command=admin_target.run, socket_path=None) is not ProbeStatus.ABSENT:
            return False
        require_unaddressed_session_absence(session, vm=vm, admin_target=admin_target)
    else:
        require_socket_path(session.socket_path, run_command=partial(admin_target.run, sudo=sudo))
        if (
            capture_tmux_server_fingerprint(target=admin_target, socket_path=session.socket_path, sudo=sudo).status
            is not ProbeStatus.ABSENT
        ):
            return False
    return _mgr._get_boot_id(admin_target) is not None


def checkpoint_session_identity(db: Database, session: SessionRow, *, vm: VMRow, admin_target: Transport) -> NoReturn:
    """Re-prove absence, record the existing boot checkpoint, and always refuse launch."""
    if not can_checkpoint_session(session, vm=vm, admin_target=admin_target):
        raise StateError(
            "runtime identity checkpoint observation changed", entity_kind="session", entity_name=session.name
        )
    _mgr._ensure_pid(session, target=admin_target, db=db)
    raise StateError(
        "runtime identity checkpoint requires a later VM boot before retry",
        entity_kind="session",
        entity_name=session.name,
    )
