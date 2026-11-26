"""Local session intent acceptance and identity checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks.db import DesiredRuntimeState
from agentworks.errors import StateError, ValidationError
from agentworks.native_locks import LockRequest, native_locks
from agentworks.native_locks.rows import session_entity_for

if TYPE_CHECKING:
    from agentworks.db import Database, SessionRow
    from agentworks.native_locks import NativeLockSet


def parse_session_desired_runtime_state(state: str | DesiredRuntimeState) -> DesiredRuntimeState:
    """Validate public session target input without loading runtime configuration."""
    try:
        parsed = DesiredRuntimeState(state)
    except (ValueError, TypeError):
        raise ValidationError("invalid session desired runtime state", entity_kind="session") from None
    if parsed is DesiredRuntimeState.ON_DEMAND:
        raise ValidationError("sessions support running or stopped desired state", entity_kind="session")
    return parsed


def accept_session_runtime_state(
    db: Database,
    session: SessionRow,
    *,
    state: DesiredRuntimeState,
    held: NativeLockSet,
) -> SessionRow:
    """Accept an identity-bound request under the caller's set, which holds the session exclusive."""
    workspace = _mgr._require_workspace(db, session.workspace_name)
    held.require(session_request(db, session), database_path=db.path)
    current = _mgr._require_session(db, session.name)
    if current.session_uuid != session.session_uuid or current.workspace_name != session.workspace_name:
        raise StateError(
            "session changed while accepting runtime intent", entity_kind="session", entity_name=session.name
        )
    _mgr._require_vm_for_workspace(db, workspace)
    db.set_session_desired_runtime_state(session.name, state, expected_session_uuid=session.session_uuid)
    return _mgr._require_session(db, session.name)


def set_session_desired_runtime_state(
    db: Database,
    *,
    name: str,
    state: str | DesiredRuntimeState,
) -> None:
    """Set a session target using only local state, under the session's exclusive lock."""
    parsed = parse_session_desired_runtime_state(state)
    session = _mgr._require_session(db, name)
    workspace = _mgr._require_workspace(db, session.workspace_name)
    _mgr._require_vm_for_workspace(db, workspace)
    with native_locks(db.path, session_request(db, session)) as held:
        accept_session_runtime_state(db, session, state=parsed, held=held)


def session_request(db: Database, session: SessionRow) -> LockRequest:
    """The session exclusive, with its workspace and account shared."""
    return LockRequest(exclusive=(session_entity_for(db, session),))
