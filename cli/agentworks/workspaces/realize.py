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
from agentworks.path_rendering import format_file_path

if TYPE_CHECKING:
    from pathlib import Path

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
) -> Path:
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
    from agentworks.agents.grants import materialize_grant_all_agents, workspace_group
    from agentworks.ssh import SSHLogger
    from agentworks.workspaces.backends.vm import (
        create_vm_workspace,
        delete_vm_workspace,
        generate_vscode_workspace,
    )

    workspace_path: str | None = None
    vscode_path: Path | None = None

    ssh_logger = SSHLogger(vm.name, "workspace-create")

    def _cleanup() -> None:
        # The on-VM teardown (directory + the workspace's fresh Linux group)
        # is gated on workspace_path, which is set only once
        # create_vm_workspace RETURNS. So it covers failures AFTER a
        # successful create (the VS Code stub, the row insert), the same
        # window the directory cleanup already covered; the group teardown
        # rides along on exactly that window. A failure mid-create (e.g. the
        # git clone) leaves workspace_path unset here, but is NOT a leak:
        # create_vm_workspace self-cleans its own group and directory before
        # re-raising (issue #262), so the two teardowns never overlap and
        # each partial-create window is reclaimed exactly once.
        if workspace_path:
            delete_vm_workspace(vm, config, name, workspace_path, workspace_group(name), logger=ssh_logger)
        if vscode_path:
            vscode_path.unlink(missing_ok=True)

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
                f"SSH log: {ssh_logger.display_path}"
            )

    # Outer try/finally ensures the SSH logger is closed exactly once, AFTER
    # any rollback commands have been logged. Closing earlier would write the
    # "Finished" footer before the rollback section, making the log misleading.
    try:
        try:
            output.info(f"Creating workspace '{name}' on VM '{vm.name}' (template: {template.name})...")
            workspace_path = create_vm_workspace(vm, config, name, template, logger=ssh_logger)

            vscode_path = generate_vscode_workspace(vm, config, name, workspace_path)
            output.detail(f"VS Code workspace: {format_file_path(vscode_path)}")

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
                hint=f"SSH log: {ssh_logger.display_path}",
            ) from e

        # Materialize grant_all agents onto the new workspace: one explicit
        # grant row plus the on-VM group membership per agent. Best-effort
        # (per-agent failures warn, they do not abort); the invariant,
        # ordering rationale, and KI handling live with the helper.
        materialize_grant_all_agents(db, config, vm, name, logger=ssh_logger)
    finally:
        ssh_logger.close()

    output.info(f"Workspace '{name}' created")
    # vscode_path was assigned inside the try before the row insert;
    # reaching here means the body completed without raising, so it is
    # set. Assert for the type-checker.
    assert vscode_path is not None
    return vscode_path
