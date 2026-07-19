"""Agent workspace grants and their on-VM Linux group counterpart.

The grant model has two halves that must not drift: the DB rows (the
explicit and implicit grants plus the agent row's grant_all flag) and
the VM's Linux group memberships (each workspace's recorded
linux_group). This module owns the commands that reconcile the two
(``agent grant-workspaces`` / ``agent revoke-workspaces`` and the
workspace-delete grant sweep) and the group-membership primitives
other domains call when they create or unwind grant-bearing state
(session create's implicit grant, agent realization's grant-all pass,
agent delete's membership cleanup).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.agents.manager import agent_scope
from agentworks.errors import NotFoundError, ValidationError
from agentworks.transports import transport
from agentworks.vms.manager import gated_vm_boundary

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.ssh import SSHLogger

WS_GROUP_PREFIX = "ws-"


def workspace_group(workspace_name: str) -> str:
    """Derive the Linux group name for a newly-created workspace: ws-<name>.

    Existing workspaces retain whatever group was stored in the database at
    their creation time (legacy workspaces use the older ws-- prefix).
    Always read workspace_row.linux_group for the canonical value; this
    helper is only used at workspace-create time.
    """
    return f"{WS_GROUP_PREFIX}{workspace_name}"


def grant_workspaces(
    db: Database,
    config: Config,
    *,
    agent_name: str,
    workspace_names: list[str],
    grant_all: bool = False,
) -> None:
    """Grant an agent explicit access to workspaces.

    Orchestrated (``vms.manager.gated_vm_boundary``): the graph is the
    live VM alone, no env-chain targets register (this command
    composes no runtime env), the activation gate replaces this
    command's ``keep_active`` (opening BEFORE the preflight sweep; its
    just-in-time values seed the boundary resolver), and the
    held-active span covers the group-membership SSH work. The
    empty-request and unknown-agent validations stay pre-gate: they
    fail with zero prompts and zero VM starts.
    """
    if not grant_all and not workspace_names:
        raise ValidationError(
            f"grant for '{agent_name}' needs at least one workspace name "
            f"or workspace_names empty + grant_all=True",
            entity_kind="agent",
            entity_name=agent_name,
        )

    agent = db.get_agent(agent_name)
    if agent is None:
        raise NotFoundError(
            f"agent '{agent_name}' not found",
            entity_kind="agent",
            entity_name=agent_name,
        )

    vm = _require_vm(db, agent.vm_name)

    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    with gated_vm_boundary(
        db, config, registry, vm, scope=agent_scope(db, vm.name, agent_name)
    ):

        if grant_all:
            db.update_agent_grant_all(agent_name, True)
            # Add to all existing workspace groups on this VM
            for ws in db.list_workspaces(vm_name=vm.name):
                add_to_workspace_group(vm, config, db, agent.linux_user, ws.name, logger=None)
                db.insert_agent_grant(agent_name, ws.name, "explicit")
            output.info(f"Agent '{agent_name}' granted access to all workspaces")
            return

        for ws_name in workspace_names:
            found_ws = db.get_workspace(ws_name)
            if found_ws is None:
                output.warn(f"workspace '{ws_name}' not found, skipping")
                continue
            add_to_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=None)
            db.insert_agent_grant(agent_name, ws_name, "explicit")
            output.detail(f"Granted: {ws_name}")


def revoke_workspaces(
    db: Database,
    config: Config,
    *,
    agent_name: str,
    workspace_names: list[str],
    revoke_all: bool = False,
) -> None:
    """Revoke explicit workspace grants from an agent.

    Orchestrated (``vms.manager.gated_vm_boundary``), mirroring
    :func:`grant_workspaces`: live-VM graph, no env-chain targets, the
    gate open before the preflight sweep, the held-active span
    covering the group-membership SSH work, and the empty-request /
    unknown-agent validations pre-gate.
    """
    if not revoke_all and not workspace_names:
        raise ValidationError(
            f"revoke for '{agent_name}' needs at least one workspace name "
            f"or workspace_names empty + revoke_all=True",
            entity_kind="agent",
            entity_name=agent_name,
        )

    agent = db.get_agent(agent_name)
    if agent is None:
        raise NotFoundError(
            f"agent '{agent_name}' not found",
            entity_kind="agent",
            entity_name=agent_name,
        )

    vm = _require_vm(db, agent.vm_name)

    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    with gated_vm_boundary(
        db, config, registry, vm, scope=agent_scope(db, vm.name, agent_name)
    ):

        if revoke_all:
            # Snapshot the granted workspaces BEFORE deleting any rows.
            # Taking it afterwards missed explicitly-granted-only
            # workspaces (their rows were already gone, so they never
            # reached the group-removal branch and the on-VM group
            # membership survived the revoke; issue #189).
            granted = db.list_granted_workspaces(agent_name)
            db.update_agent_grant_all(agent_name, False)
            db.delete_explicit_grants(agent_name)
            # Remove from groups where no grant (implicit or grant-all) remains
            remaining_implicit: list[str] = []
            for ws_name in granted:
                if db.has_any_grant(agent_name, ws_name):
                    remaining_implicit.append(ws_name)
                else:
                    remove_from_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=None)
            output.info(f"All explicit grants revoked for agent '{agent_name}'")
            if remaining_implicit:
                output.warn(
                    f"agent still has implicit access via sessions to: {', '.join(remaining_implicit)}"
                )
            return

        for ws_name in workspace_names:
            db.delete_agent_grant(agent_name, ws_name, "explicit")
            if not db.has_any_grant(agent_name, ws_name):
                remove_from_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=None)
                output.detail(f"Revoked: {ws_name}")
            else:
                output.detail(f"Revoked: {ws_name} (still has implicit access via sessions)")


def revoke_workspace_grants(
    db: Database,
    config: Config,
    ws_name: str,
    vm: VMRow,
) -> None:
    """Remove all agent grants for a workspace (called during workspace deletion).

    Agents are VM-scoped and not deleted with workspaces. Only their grants
    and group memberships for this workspace are removed.
    """
    # Find agents that have grants for this workspace
    # We need to remove group membership for each
    from agentworks.ssh import SSHLogger

    ssh_logger = SSHLogger(vm.name, "workspace-delete-grants")
    agents = db.list_agents(vm_name=vm.name)
    for agent in agents:
        if db.has_any_grant(agent.name, ws_name):
            remove_from_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=ssh_logger)
    ssh_logger.close()


# -- Group-membership primitives -------------------------------------------


def _resolve_ws_group(db: Database, workspace_name: str) -> str:
    """Look up the Linux group stored for a workspace.

    Callers must use the recorded group rather than re-deriving it, so
    legacy workspaces (with the older ws-- prefix on their on-VM group)
    keep working after the prefix change.
    """
    ws = db.get_workspace(workspace_name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{workspace_name}' not found",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    return ws.linux_group


def add_to_workspace_group(
    vm: VMRow,
    config: Config,
    db: Database,
    linux_user: str,
    workspace_name: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Add an agent user to a workspace's Linux group."""
    target = transport(vm, config, logger=logger)
    ws_grp = _resolve_ws_group(db, workspace_name)
    # Ensure group exists (idempotent)
    target.run(f"sh -c 'getent group {ws_grp} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_grp}'", sudo=True)
    target.run(f"usermod -aG {ws_grp} {linux_user}", sudo=True)


def remove_from_workspace_group(
    vm: VMRow,
    config: Config,
    db: Database,
    linux_user: str,
    workspace_name: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Remove an agent user from a workspace's Linux group."""
    target = transport(vm, config, logger=logger)
    ws_grp = _resolve_ws_group(db, workspace_name)
    target.run(f"gpasswd -d {linux_user} {ws_grp}", sudo=True, check=False)


# -- Helpers ---------------------------------------------------------------


def _require_vm(db: Database, vm_name: str) -> VMRow:
    vm = db.get_vm(vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{vm_name}' not found",
            entity_kind="vm",
            entity_name=vm_name,
        )
    return vm
