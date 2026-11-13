"""Local session intent acceptance and identity checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks.db import DesiredRuntimeState
from agentworks.errors import StateError, ValidationError
from agentworks.harness_setup.locking import native_mutation_guard

if TYPE_CHECKING:
    from agentworks.db import Database, SessionRow
    from agentworks.harness_setup.locking import NativeMutationGuard


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
    held_native_guard: NativeMutationGuard,
) -> SessionRow:
    """Accept an identity-bound request under the caller's family guard."""
    workspace = _mgr._require_workspace(db, session.workspace_name)
    with native_mutation_guard(db.path, workspace.vm_name, held=held_native_guard):
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
    """Set a session target using only local state and the native family guard."""
    parsed = parse_session_desired_runtime_state(state)
    session = _mgr._require_session(db, name)
    workspace = _mgr._require_workspace(db, session.workspace_name)
    vm = _mgr._require_vm_for_workspace(db, workspace)
    with native_mutation_guard(db.path, vm.name) as guard:
        accept_session_runtime_state(db, session, state=parsed, held_native_guard=guard)
