"""Agent workspace grants and their on-VM Linux group counterpart.

The grant model has two halves that must not drift: the DB rows (the
explicit and implicit grants plus the agent row's grant_all flag) and
the VM's Linux group memberships (each workspace's recorded
linux_group). This module owns the commands that reconcile the two
(``agent grant-workspaces`` / ``agent revoke-workspaces`` and the
workspace-delete grant sweep), the group-membership primitives
other domains call when they create or unwind grant-bearing state
(session create's implicit grant, agent realization's grant-all pass,
agent delete's membership cleanup), and the grant-all materialization
pass every workspace-inserting path runs
(:func:`materialize_grant_all_agents`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.agents.manager import agent_scope
from agentworks.errors import NotFoundError, ValidationError
from agentworks.naming import LINUX_GROUPNAME_MAX_LENGTH
from agentworks.secrets.policy import InteractionPolicy, validate_interaction_policy
from agentworks.transports import transport
from agentworks.vms.manager import gated_vm_boundary

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.ssh import SSHLogger

WS_GROUP_PREFIX = "ws-"

# Derived FROM the prefix so a prefix change cannot reintroduce an over-limit
# Linux group. ``workspace_group`` builds ``ws-<name>``, which must fit the
# 32-char Linux group limit: 32 - len("ws-") = 29. The ceiling is imported from
# the config layer (never the reverse); config.validation depends on nothing in
# the agents package, so this cannot cycle.
MAX_WORKSPACE_NAME_LENGTH = LINUX_GROUPNAME_MAX_LENGTH - len(WS_GROUP_PREFIX)


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
    interaction: InteractionPolicy,
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
    interaction = validate_interaction_policy(interaction)
    if not grant_all and not workspace_names:
        raise ValidationError(
            f"grant for '{agent_name}' needs at least one workspace name or workspace_names empty + grant_all=True",
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

    from agentworks.bootstrap import load_request_registry

    registry = load_request_registry(config)
    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        scope=agent_scope(db, vm.name, agent_name),
        interaction=interaction,
    ):
        if grant_all:
            db.update_agent_grant_all(agent_name, True)
            # Add to all existing workspace groups on this VM
            for ws in db.list_workspaces(vm_name=vm.name):
                add_to_workspace_group(vm, config, db, agent.linux_user, ws.name, logger=None)
                db.insert_agent_grant(agent_name, ws.name, "explicit")
            output.result(f"Agent '{agent_name}' granted access to all workspaces")
            return

        granted = 0
        for ws_name in workspace_names:
            found_ws = db.get_workspace(ws_name)
            if found_ws is None:
                output.warn(f"workspace '{ws_name}' not found, skipping")
                continue
            add_to_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=None)
            db.insert_agent_grant(agent_name, ws_name, "explicit")
            output.info(f"Granted: {ws_name}")
            granted += 1
        output.result(f"Agent '{agent_name}' granted access to {output.count(granted, 'workspace')}")


def revoke_workspaces(
    db: Database,
    config: Config,
    *,
    agent_name: str,
    workspace_names: list[str],
    revoke_all: bool = False,
    interaction: InteractionPolicy,
) -> None:
    """Revoke explicit workspace grants from an agent.

    Orchestrated (``vms.manager.gated_vm_boundary``), mirroring
    :func:`grant_workspaces`: live-VM graph, no env-chain targets, the
    gate open before the preflight sweep, the held-active span
    covering the group-membership SSH work, and the empty-request /
    unknown-agent validations pre-gate.
    """
    interaction = validate_interaction_policy(interaction)
    if not revoke_all and not workspace_names:
        raise ValidationError(
            f"revoke for '{agent_name}' needs at least one workspace name or workspace_names empty + revoke_all=True",
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

    from agentworks.bootstrap import load_request_registry

    registry = load_request_registry(config)
    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        scope=agent_scope(db, vm.name, agent_name),
        interaction=interaction,
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
            output.result(f"All explicit grants revoked for agent '{agent_name}'")
            if remaining_implicit:
                output.warn(f"agent still has implicit access via sessions to: {', '.join(remaining_implicit)}")
            return

        for ws_name in workspace_names:
            db.delete_agent_grant(agent_name, ws_name, "explicit")
            if not db.has_any_grant(agent_name, ws_name):
                remove_from_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=None)
                output.info(f"Revoked: {ws_name}")
            else:
                output.info(f"Revoked: {ws_name} (still has implicit access via sessions)")
        output.result(f"Revoked {output.count(len(workspace_names), 'workspace grant')} from agent '{agent_name}'")


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


# -- Grant-all materialization ---------------------------------------------


def materialize_grant_all_agents(
    db: Database,
    config: Config,
    vm: VMRow,
    workspace_name: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Materialize every grant_all agent on ``vm`` onto the just-created
    workspace ``workspace_name``: one explicit grant row plus the on-VM
    group membership per agent.

    Every path that inserts a workspace row must run this pass, so the
    invariant holds that a grant_all agent has a grant row for every
    workspace on its VM (issue #321). Agent reinit's reconcile (#280)
    replays ON-VM state from the recorded rows, so grant_all agents would
    silently lack access to a workspace this pass skipped, and even a
    reinit could not restore it.

    Best-effort: the workspace itself was already created and inserted by
    the caller, so a per-agent failure (DB error, SSH hiccup) does not
    abort the whole command. Failures surface as warnings with accurate
    counts so the user can re-grant manually with
    'agent grant-workspaces'.

    The DB grant is inserted BEFORE the on-VM group add. If the order
    were reversed and the DB write failed after the group add, the agent
    would have VM-side membership with no DB grant backing it (a silent
    authorization drift). With this ordering, a group-add failure can be
    cleanly compensated by deleting the just-inserted grant row.
    """
    grant_all_agents = db.list_agents_on_vm_with_grant_all(vm.name)
    if not grant_all_agents:
        return
    added = 0
    failed: list[str] = []
    for agent in grant_all_agents:
        try:
            db.insert_agent_grant(agent.name, workspace_name, "explicit")
        except KeyboardInterrupt:
            # sqlite commits inside a C call; KI can surface after the
            # commit but before we move on, leaving an inserted row.
            # Best-effort revert and re-raise to preserve the SIGINT
            # contract.
            output.warn(
                f"Cancelled while inserting grant for agent '{agent.name}' on "
                f"workspace '{workspace_name}'. Reverting in case the insert committed."
            )
            _revert_grant_on_failure(db, agent.name, workspace_name)
            raise
        except Exception as e:
            failed.append(agent.name)
            output.warn(f"Failed to insert grant for agent '{agent.name}' on workspace '{workspace_name}': {e}")
            continue
        try:
            add_to_workspace_group(vm, config, db, agent.linux_user, workspace_name, logger=logger)
            added += 1
        except KeyboardInterrupt:
            # KI is a BaseException and slips past `except Exception`,
            # so it needs its own branch. Without this, Ctrl-C during
            # the SSH call would leave a committed grant row with no
            # VM-side group membership (silent authorization drift).
            output.warn(
                f"Cancelled while adding agent '{agent.name}' to workspace "
                f"'{workspace_name}' group. Reverting just-inserted DB grant."
            )
            _revert_grant_on_failure(db, agent.name, workspace_name)
            raise
        except Exception as e:
            failed.append(agent.name)
            output.warn(
                f"Failed to add agent '{agent.name}' to workspace '{workspace_name}' "
                f"group: {e}. Reverting DB grant to keep state consistent."
            )
            _revert_grant_on_failure(db, agent.name, workspace_name)
    if added:
        output.detail(f"Added {added} grant-all agent(s) to workspace")
    if failed:
        output.warn(
            f"Grant-all agents not added: {', '.join(failed)}. "
            f"Re-grant manually with 'agent grant-workspaces <name> {workspace_name}'."
        )


def _revert_grant_on_failure(db: Database, agent_name: str, ws_name: str) -> None:
    """Best-effort: drop a just-inserted explicit grant after the on-VM
    group add failed (or was cancelled). Used by
    :func:`materialize_grant_all_agents` to keep DB and VM authorization
    aligned. A failure to revert is logged but does not raise, so it
    never masks the caller's original exception (or KeyboardInterrupt)."""
    try:
        db.delete_agent_grant(agent_name, ws_name, "explicit")
    except Exception as revert_err:
        output.warn(
            f"Could not revert grant for '{agent_name}' on workspace '{ws_name}': "
            f"{revert_err}. DB has a grant row with no VM-side group membership; "
            f"re-run 'agent grant-workspaces {agent_name} {ws_name}' or "
            f"revoke explicitly."
        )


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
