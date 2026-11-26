"""The lock sets of workspace operations, and the workspace trees they lock through the guest's lookup walk.

A workspace operation locks its workspace and grants from rows it read at
step 1, and every tree it creates, fills, removes or reads whole as a
``path`` entity named by the walk at step 5 (``native_locks.path_walk``).
The walk runs again under the set (step 7); a different result refuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.native_locks import LockRequest, grant_all_entity
from agentworks.native_locks.rows import (
    admin_account_entity,
    agent_account_entity,
    grant_entity_for,
    path_entities,
    workspace_entity_for,
)
from agentworks.recheck import require_unchanged

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.config import Config
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.native_locks import Entity
    from agentworks.native_locks.path_walk import PathWalk


@dataclass(frozen=True)
class TreeLocks:
    """The walked trees of one operation on one VM: the paths it asked for, how it walked them, and what they name."""

    vm: str
    paths: tuple[str, ...]
    unlink_only: bool
    walks: tuple[PathWalk, ...]

    def exclusive(self) -> tuple[Entity, ...]:
        """Each tree's final path (for a delete, the entry it removes)."""
        return tuple(path_entities(self.vm, walk)[0] for walk in self.walks)

    def shared(self) -> tuple[Entity, ...]:
        """Every directory and followed link the walks looked a name up in."""
        return tuple(entity for walk in self.walks for entity in path_entities(self.vm, walk)[1])


def walk_trees(vm: VMRow, config: Config, paths: Sequence[str], *, unlink_only: bool = False) -> TreeLocks:
    """Walk ``paths`` (stored spellings) on ``vm`` as the admin, at step 5."""
    return TreeLocks(vm.name, tuple(paths), unlink_only, _walk(vm, config, paths, unlink_only=unlink_only))


def _walk(vm: VMRow, config: Config, paths: Sequence[str], *, unlink_only: bool) -> tuple[PathWalk, ...]:
    """Run the guest's lookup walk for ``paths`` through the admin's access."""
    from agentworks import execution
    from agentworks.execution.access import Admin
    from agentworks.native_locks.path_walk import walk_paths

    return walk_paths(execution.vm_access(vm, config, Admin()), paths, unlink_only=unlink_only)


def require_trees_unchanged(vm: VMRow, config: Config, trees: TreeLocks) -> None:
    """Under the set: walk the same paths again and refuse if any directory, link or final path moved."""
    again = walk_trees(vm, config, trees.paths, unlink_only=trees.unlink_only)
    require_unchanged("vm", vm.name, trees.walks, again.walks)


def with_trees(request: LockRequest, *trees: TreeLocks) -> LockRequest:
    """``request`` with the walked trees joined: their final paths exclusive, their lookups shared."""
    exclusive = tuple(entity for tree in trees for entity in tree.exclusive())
    shared = tuple(entity for tree in trees for entity in tree.shared())
    return LockRequest(exclusive=(*request.exclusive, *exclusive), shared=(*request.shared, *shared))


def workspace_create_request(db: Database, vm: VMRow, name: str) -> LockRequest:
    """Creating workspace ``name``: its entity, the admin's grant and one per grant-all agent, with grant-all shared.

    The workspace is locked by name before its row exists, so two creates
    of one name refuse each other.
    """
    from agentworks.native_locks import grant_entity, workspace_entity

    workspace = workspace_entity(vm.name, name)
    agents = db.list_agents_on_vm_with_grant_all(vm.name)
    grants = (
        grant_entity(workspace, admin_account_entity(vm)),
        *(grant_entity(workspace, agent_account_entity(agent)) for agent in agents),
    )
    return LockRequest(exclusive=(workspace, *grants), shared=(grant_all_entity(vm.name),))


def workspace_grants(db: Database, vm: VMRow, workspace: WorkspaceRow) -> tuple[Entity, ...]:
    """The admin's grant on ``workspace`` and one per agent holding a grant there."""
    accounts = [admin_account_entity(vm)]
    for agent in db.list_agents(vm_name=vm.name):
        if workspace.name in db.list_granted_workspaces(agent.name):
            accounts.append(agent_account_entity(agent))
    return tuple(grant_entity_for(workspace, account) for account in accounts)


def workspace_request(workspace: WorkspaceRow, *more: Entity) -> LockRequest:
    """``workspace`` exclusive, with ``more`` exclusive beside it."""
    return LockRequest(exclusive=(workspace_entity_for(workspace), *more))
