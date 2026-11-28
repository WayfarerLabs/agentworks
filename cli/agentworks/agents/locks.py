"""The lock sets of agent operations, built from the rows a command read.

Each builder returns only the request. A command builds it at step 1,
probes it, and once it holds it rebuilds it from a fresh read and compares
the two (``recheck.require_request_unchanged``), so a grant, workspace,
session or console that came or went in between refuses the command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.native_locks import LockRequest, account_entity, grant_all_entity, grant_entity, workspace_entity
from agentworks.native_locks.rows import (
    agent_account_entity,
    consoles_holding,
    session_entity_for,
    workspace_entity_for,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.db import AgentRow, Database, SessionRow, VMRow


def agent_create_request(db: Database, vm: VMRow, name: str, *, grant_all: bool) -> LockRequest:
    """Creating agent ``name``: its account exclusive, and for a grant-all agent the grant-all set and every grant.

    The account is locked by the Linux user the agent will have, before it
    exists, so two creates of one name refuse each other. A grant-all agent
    takes a grant for every workspace on the VM.
    """
    from agentworks.agents.manager import derive_linux_user

    account = account_entity(vm.name, derive_linux_user(name))
    if not grant_all:
        return LockRequest(exclusive=(account,))
    grants = tuple(grant_entity(workspace_entity_for(ws), account) for ws in db.list_workspaces(vm_name=vm.name))
    return LockRequest(exclusive=(account, grant_all_entity(vm.name), *grants))


def agent_delete_request(db: Database, agent: AgentRow, sessions: Sequence[SessionRow]) -> LockRequest:
    """Deleting ``agent`` and ``sessions``: the account, the sessions, its grants and their consoles, all exclusive.

    A grant-all agent also takes the grant-all set.
    """
    account = agent_account_entity(agent)
    exclusive = [
        account,
        *(session_entity_for(db, session) for session in sessions),
        *(grant_entity(workspace_entity(agent.vm_name, ws), account) for ws in db.list_granted_workspaces(agent.name)),
        *consoles_holding(db, sessions),
    ]
    if agent.grant_all:
        exclusive.append(grant_all_entity(agent.vm_name))
    return LockRequest(exclusive=tuple(exclusive))


def agent_reinit_request(db: Database, agent: AgentRow) -> LockRequest:
    """Reinitializing ``agent``: its account and a grant per recorded grant it replays, all exclusive."""
    account = agent_account_entity(agent)
    grants = tuple(
        grant_entity(workspace_entity(agent.vm_name, ws), account) for ws in db.list_granted_workspaces(agent.name)
    )
    return LockRequest(exclusive=(account, *grants))


def grant_request(
    db: Database, vm: VMRow, agent: AgentRow, workspace_names: Sequence[str], *, every: bool
) -> LockRequest:
    """A grant or revoke: each named workspace's grant, or with ``every`` grant-all and every grant, all exclusive.

    Each named workspace's grant is locked by name, whether or not it
    exists. The account is held shared either way, even with no workspace,
    so an agent delete refuses it.
    """
    account = agent_account_entity(agent)
    if every:
        names = tuple(ws.name for ws in db.list_workspaces(vm_name=vm.name))
        exclusive = (grant_all_entity(vm.name), *(grant_entity(workspace_entity(vm.name, n), account) for n in names))
        return LockRequest(exclusive=exclusive, shared=(account,))
    grants = tuple(grant_entity(workspace_entity(vm.name, name), account) for name in workspace_names)
    return LockRequest(exclusive=grants, shared=(account,))
