"""The phase-free workspace realization body.

:func:`realize_workspace` is the choreography that makes a workspace
REAL: the bespoke mutation an orchestrator runs in its roll-forward,
between the boundary resolve and ``log.mark_realized``. It is domain
code with deliberately narrow duties: it frames no phases, resolves no
secrets, opens no gate, and re-checks nothing its caller already
validated (name shape, existence, VM anchoring). The calling
orchestrator owns all of that; this body owns only the mutation and
the mutation's own partial-state cleanup (files or a VS Code stub
written before the failure), which it unwinds itself before
re-raising. Rollback of a COMPLETED workspace is not this function's
job either: that is the pending workspace node's ``teardown``, driven
by the orchestrator's realization log.

Parity oracle: the mutation slice of the imperative ``workspaces
.manager.create_workspace``, exactly as ``session create
--new-workspace`` invoked it nested at the time this body was factored
out (same messages, same error wrapping, same grant-all
reconciliation, minus the nested command root's own registry build,
re-validation, and re-gate). The standalone ``workspace create`` now
runs this body too, so it is the SINGLE copy of the slice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import AgentworksError, ExternalError

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.resources.registry import Registry
    from agentworks.workspaces.templates import ResolvedTemplate


def realize_workspace(
    db: Database,
    config: Config,
    registry: Registry,
    *,
    name: str,
    vm: VMRow,
    template: ResolvedTemplate,
) -> str:
    """Make workspace ``name`` real on ``vm``: create the on-VM
    directory from its RESOLVED template, generate the VS Code
    workspace stub, insert the DB row, and reconcile grant-all agents
    onto the new workspace's group.

    The template arrives resolved and the VM pre-validated: cheap
    config- and row-based checks (template resolution, the repo
    advisories, the VM init-status guard) are the calling
    orchestrator's pre-gate duty, so their failures never cost a
    prompt or a VM start; this body is only the mutation.

    Returns the VS Code workspace stub path, for callers with an
    open-in-VS-Code tail; callers without one ignore it. Raises on
    failure AFTER unwinding its own partial state; the caller's
    realization log never sees a half-made workspace.
    """
    from agentworks.agents.manager import _add_to_workspace_group, workspace_group
    from agentworks.ssh import SSHLogger
    from agentworks.workspaces.backends.vm import (
        create_vm_workspace,
        delete_vm_workspace,
        generate_vscode_workspace,
    )
    from agentworks.workspaces.manager import _revert_grant_on_failure

    workspace_path: str | None = None
    vscode_path: str | None = None

    ssh_logger = SSHLogger(vm.name, "workspace-create")

    def _cleanup() -> None:
        if workspace_path:
            delete_vm_workspace(vm, config, name, workspace_path, logger=ssh_logger)
        if vscode_path:
            from pathlib import Path

            Path(vscode_path).unlink(missing_ok=True)

    def _safe_cleanup() -> None:
        # Rollback failures must not mask the original KI/exception. Surface
        # them as a warning (with workspace name and SSH log path for the
        # user to follow up on) and continue propagating the original error.
        try:
            _cleanup()
        except Exception as cleanup_err:
            output.warn(
                f"rollback during workspace create '{name}' failed: {cleanup_err}. "
                f"VM may have residual files or VS Code workspace file. "
                f"SSH log: {ssh_logger.path}"
            )

    # Outer try/finally ensures the SSH logger is closed exactly once, AFTER
    # any rollback commands have been logged. Closing earlier would write the
    # "Finished" footer before the rollback section, making the log misleading.
    try:
        try:
            output.info(
                f"Creating workspace '{name}' on VM '{vm.name}' (template: {template.name})..."
            )
            workspace_path = create_vm_workspace(vm, config, name, template, logger=ssh_logger)

            vscode_path = generate_vscode_workspace(vm, config, name, workspace_path)
            output.detail(f"VS Code workspace: {vscode_path}")

            db.insert_workspace(
                name,
                workspace_path=workspace_path,
                vm_name=vm.name,
                template=template.name,
                linux_group=workspace_group(name),
            )
        except KeyboardInterrupt:
            output.warn(f"Cancelling workspace create '{name}'... rolling back.")
            _safe_cleanup()
            raise
        except AgentworksError:
            _safe_cleanup()
            raise
        except Exception as e:
            _safe_cleanup()
            raise ExternalError(
                f"creating workspace: {e}",
                entity_kind="workspace",
                entity_name=name,
                hint=f"SSH log: {ssh_logger.path}",
            ) from e

        # Add grant_all agents to the new workspace group. Best-effort: the
        # workspace itself was already created and inserted above, so a
        # per-agent failure (DB error, SSH hiccup) should not abort the
        # whole command. Surface failures as warnings and report accurate
        # counts so the user can re-grant manually with
        # 'agent grant-workspaces'.
        #
        # DB grant is inserted BEFORE the on-VM group add. If the order were
        # reversed and the DB write failed after the group add, the agent
        # would have VM-side membership with no DB grant backing it (a
        # silent authorization drift). With this ordering, a group-add
        # failure can be cleanly compensated by deleting the just-inserted
        # grant row.
        grant_all_agents = db.list_agents_on_vm_with_grant_all(vm.name)
        if grant_all_agents:
            added = 0
            failed: list[str] = []
            for agent in grant_all_agents:
                try:
                    db.insert_agent_grant(agent.name, name, "explicit")
                except KeyboardInterrupt:
                    # sqlite commits inside a C call; KI can surface after
                    # the commit but before we move on, leaving an inserted
                    # row. Best-effort revert and re-raise to preserve the
                    # SIGINT contract.
                    output.warn(
                        f"Cancelled while inserting grant for agent '{agent.name}' on "
                        f"workspace '{name}'. Reverting in case the insert committed."
                    )
                    _revert_grant_on_failure(db, agent.name, name)
                    raise
                except Exception as e:
                    failed.append(agent.name)
                    output.warn(
                        f"Failed to insert grant for agent '{agent.name}' on workspace "
                        f"'{name}': {e}"
                    )
                    continue
                try:
                    _add_to_workspace_group(
                        vm, config, db, agent.linux_user, name, logger=ssh_logger
                    )
                    added += 1
                except KeyboardInterrupt:
                    # KI is a BaseException and slips past `except Exception`,
                    # so it needs its own branch. Without this, Ctrl-C during
                    # the SSH call would leave a committed grant row with no
                    # VM-side group membership (silent authorization drift).
                    output.warn(
                        f"Cancelled while adding agent '{agent.name}' to workspace "
                        f"'{name}' group. Reverting just-inserted DB grant."
                    )
                    _revert_grant_on_failure(db, agent.name, name)
                    raise
                except Exception as e:
                    failed.append(agent.name)
                    output.warn(
                        f"Failed to add agent '{agent.name}' to workspace '{name}' "
                        f"group: {e}. Reverting DB grant to keep state consistent."
                    )
                    _revert_grant_on_failure(db, agent.name, name)
            if added:
                output.detail(f"Added {added} grant-all agent(s) to workspace")
            if failed:
                output.warn(
                    f"Grant-all agents not added: {', '.join(failed)}. "
                    f"Re-grant manually with 'agent grant-workspaces <name> {name}'."
                )
    finally:
        ssh_logger.close()

    output.info(f"Workspace '{name}' created")
    # vscode_path was assigned inside the try before the row insert;
    # reaching here means the body completed without raising, so it is
    # set. Assert for the type-checker.
    assert vscode_path is not None
    return vscode_path
