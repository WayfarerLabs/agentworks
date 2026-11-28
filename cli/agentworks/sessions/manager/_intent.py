"""Session lock sets, local intent acceptance and identity checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks.db import DesiredRuntimeState
from agentworks.errors import StateError, ValidationError
from agentworks.native_locks import LockRequest, native_locks
from agentworks.native_locks.rows import session_entity_for

if TYPE_CHECKING:
    from agentworks.db import Database, SessionRow
    from agentworks.native_locks import Entity, NativeLockSet

    from ._create_types import SessionPlan


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


def session_create_request(db: Database, plan: SessionPlan) -> LockRequest:
    """Session create's lock set, without a new workspace's tree.

    The session, and in agent mode its implicit grant, exclusive; a new
    workspace adds what workspace create takes (the workspace, the admin's
    grant and the grant-all agents' grants, with grant-all shared), and a
    new agent its account. Each new entity is locked by name before its row
    exists.
    """
    from agentworks.agents.manager import derive_linux_user
    from agentworks.native_locks import LockRequest, account_entity, grant_entity, session_entity, workspace_entity
    from agentworks.native_locks.rows import admin_account_entity, agent_account_entity
    from agentworks.workspaces.locks import workspace_create_request

    vm = plan.vm
    workspace = workspace_entity(vm.name, plan.workspace_name)
    if plan.agent_name is None:
        account = admin_account_entity(vm)
    elif plan.new_agent or plan.existing_agent is None:
        account = account_entity(vm.name, derive_linux_user(plan.agent_name))
    else:
        account = agent_account_entity(plan.existing_agent)
    exclusive = [session_entity(plan.name, workspace=workspace, account=account)]
    shared: list[Entity] = []
    if plan.agent_name is not None:
        exclusive.append(grant_entity(workspace, account))
    if plan.new_agent:
        exclusive.append(account)
    if plan.new_workspace:
        created = workspace_create_request(db, vm, plan.workspace_name)
        exclusive.extend(created.exclusive)
        shared.extend(created.shared)
    return LockRequest(exclusive=tuple(exclusive), shared=tuple(shared))


def session_delete_request(db: Database, session: SessionRow) -> LockRequest:
    """Session delete's set: the session, in agent mode its implicit grant, and each console holding it, exclusive."""
    from agentworks.native_locks import LockRequest
    from agentworks.native_locks.rows import consoles_holding, session_entity_for, session_grant_entity

    grant = session_grant_entity(db, session)
    return LockRequest(
        exclusive=(
            session_entity_for(db, session),
            *(() if grant is None else (grant,)),
            *consoles_holding(db, [session]),
        )
    )
