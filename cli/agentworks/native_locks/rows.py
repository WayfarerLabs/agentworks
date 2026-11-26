"""Lock entities built from database rows and from the guest's lookup walk.

Every builder takes rows a command read before locking (step 1) and names
the entities its request holds. A row a builder needs but cannot find is a
concurrent change the command would otherwise trip over later, so it raises
``StateConflictError`` naming the missing row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateConflictError, StateError

from ._entities import (
    Entity,
    LockRequest,
    account_entity,
    console_entity,
    grant_entity,
    path_entity,
    session_entity,
    vm_entity,
    workspace_entity,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agentworks.db import AgentRow, ConsoleRow, Database, SessionRow, VMRow, WorkspaceRow

    from .path_walk import PathWalk

_CHANGED_HINT = "Run the command again."


def vm_exclusive(vm: str) -> LockRequest:
    """The request of a VM-wide operation: the vm exclusive."""
    return LockRequest(exclusive=(vm_entity(vm),))


def admission(vm: str) -> LockRequest:
    """An access command's admission request: the vm shared, and nothing else."""
    return LockRequest(shared=(vm_entity(vm),))


def workspace_entity_for(workspace: WorkspaceRow) -> Entity:
    """The workspace's own entity."""
    return workspace_entity(workspace.vm_name, workspace.name)


def agent_account_entity(agent: AgentRow) -> Entity:
    """The agent's account, keyed by its Linux user."""
    return account_entity(agent.vm_name, agent.linux_user)


def admin_account_entity(vm: VMRow) -> Entity:
    """The VM admin's account."""
    return account_entity(vm.name, vm.admin_username)


def grant_entity_for(workspace: WorkspaceRow, account: Entity) -> Entity:
    """``account``'s membership in ``workspace``'s group."""
    return grant_entity(workspace_entity_for(workspace), account)


def session_entity_for(db: Database, session: SessionRow) -> Entity:
    """A session below its workspace and its account: its agent's, or the VM admin's in admin mode."""
    workspace = _workspace_of(db, session)
    return session_entity(
        session.name, workspace=workspace_entity_for(workspace), account=_account_in(db, session, workspace)
    )


def session_account(db: Database, session: SessionRow) -> Entity:
    """The account a session runs as."""
    return _account_in(db, session, _workspace_of(db, session))


def session_grant_entity(db: Database, session: SessionRow) -> Entity | None:
    """The implicit grant an agent-mode session holds on its workspace, or ``None`` in admin mode."""
    if session.agent_name is None:
        return None
    workspace = _workspace_of(db, session)
    return grant_entity_for(workspace, _account_in(db, session, workspace))


def _workspace_of(db: Database, session: SessionRow) -> WorkspaceRow:
    workspace = db.get_workspace(session.workspace_name)
    if workspace is None:
        raise _vanished("workspace", session.workspace_name)
    return workspace


def _account_in(db: Database, session: SessionRow, workspace: WorkspaceRow) -> Entity:
    """The account ``session`` runs as, on ``workspace``'s VM."""
    if session.agent_name is not None:
        agent = db.get_agent(session.agent_name)
        if agent is None:
            raise _vanished("agent", session.agent_name)
        return agent_account_entity(agent)
    vm = db.get_vm(workspace.vm_name)
    if vm is None:
        raise _vanished("vm", workspace.vm_name)
    return admin_account_entity(vm)


def console_entity_for(db: Database, console: ConsoleRow) -> Entity:
    """A console below the vm and every session it holds now."""
    members = []
    for member in db.list_console_sessions(console.name):
        session = db.get_session(member.session_name)
        if session is None:
            raise _vanished("session", member.session_name)
        members.append(session_entity_for(db, session))
    return console_entity(console.vm_name, console.name, members=members)


def consoles_holding(db: Database, sessions: Iterable[SessionRow]) -> tuple[Entity, ...]:
    """Every console holding any of ``sessions``, each once."""
    consoles: dict[str, Entity] = {}
    for session in sessions:
        for console in db.list_consoles_for_session(session.name):
            if console.name not in consoles:
                consoles[console.name] = console_entity_for(db, console)
    return tuple(consoles.values())


def path_entities(vm: str, walk: PathWalk) -> tuple[Entity, tuple[Entity, ...]]:
    """The walk's final path, held exclusive, and every directory and followed link it recorded, held shared.

    A path that resolves to ``/`` is refused as a plain ``StateError``: it
    names no tree any operation may hold, which is not a change since a read.
    """
    if walk.final == "/":
        raise StateError(
            f"workspace path {walk.path!r} on VM '{vm}' resolves to '/'",
            entity_kind="vm",
            entity_name=vm,
            hint="Store the workspace at a directory below '/'.",
        )
    return path_entity(vm, walk.final), tuple(path_entity(vm, name) for name in (*walk.directories, *walk.links))


def _vanished(kind: str, name: str) -> StateConflictError:
    return StateConflictError(
        f"{kind} '{name}' no longer exists in the database", entity_kind=kind, entity_name=name, hint=_CHANGED_HINT
    )
