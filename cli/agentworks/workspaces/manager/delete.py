"""Workspace deletion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import NotFoundError, StateError, UserAbort
from agentworks.vms.manager import gated_vm_boundary
from agentworks.workspaces.manager._common import _workspace_scope

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.transports import Transport
    from agentworks.vms.nodes import LiveVMNode


def delete_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    force: bool = False,
    yes: bool = False,
    vm_node: LiveVMNode | None = None,
) -> None:
    """Delete a workspace.

    Orchestrated on the standalone path (``vm_node=None``, the command
    root and ``delete_session``'s workspace-cleanup call):
    ``vms.manager.gated_vm_boundary`` composes the live-VM graph at
    WORKSPACE scope, the activation gate's held-active span covers the
    session-kill and on-VM removal work. The sessions guard, the confirm
    gate, and the not-found check stay pre-boundary: a refusal costs
    zero prompts, zero resolves, and zero gate events. A missing VM row
    skips the boundary entirely (DB-only cleanup), and a VM without a
    Tailscale address skips only the SSH session-kill block, exactly
    the imperative shape.

    ``vm_node`` is the nested-teardown path (session create's ephemeral
    ROLLBACK, where ``PendingWorkspaceNode.teardown`` runs INSIDE the
    caller's held activation gate). That gate already converged the VM
    and holds it active across the whole unwind, so this path composes
    NO second boundary and resolves NOTHING: it trusts the caller's
    gate and re-enters only the keepalive hold, reaching the platform
    through the node's own site edge. Passing the node (never a bare
    platform) is what keeps a teardown from silently falling into the
    boundary-building standalone branch.
    """

    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )

    # Check for sessions
    session_count = len(db.list_sessions(workspace_name=name))
    if session_count > 0 and not force:
        raise StateError(
            f"workspace '{name}' has {session_count} session(s).",
            entity_kind="workspace",
            entity_name=name,
            hint="Delete the sessions first, or pass --force to also delete them.",
        )

    if not yes:
        msg = f"Delete workspace '{name}'?"
        if session_count > 0:
            msg += f" ({session_count} session(s) will also be deleted)"
        if not output.confirm(msg):
            raise UserAbort("delete cancelled")

    # Create SSH logger for VM operations
    import contextlib

    from agentworks.ssh import SSHLogger

    ssh_logger = SSHLogger(ws.vm_name, "workspace-delete")
    output.info(f"Deleting workspace '{name}' on VM '{ws.vm_name}'...")

    # Kill running sessions (status-aware) and delete session records
    vm = db.get_vm(ws.vm_name)
    # console_pairs is populated only when we have live SSH access; the
    # post-delete cleanup is best-effort and skips when target is None.
    target: Transport | None = None
    console_pairs: list[tuple[str, str]] = []
    with contextlib.ExitStack() as _keepalive_stack:
        if vm is not None:
            if vm_node is None:
                # The standalone composition root: build the boundary here.
                from agentworks.bootstrap import build_registry

                registry = build_registry(config)
                _keepalive_stack.enter_context(
                    gated_vm_boundary(
                        db,
                        config,
                        registry,
                        vm,
                        scope=_workspace_scope(db, vm, name),
                    )
                )
            else:
                # The nested-teardown path: the caller's composition
                # already converged the VM and holds its activation gate
                # open across this unwind, so we compose no second
                # boundary and resolve nothing; we re-enter only the
                # keepalive hold, reaching the platform through the
                # node's own site edge.
                #
                # That hold keeps the NODE's VM active, but the delete
                # body issues its SSH + DB work against the workspace's
                # own VM (``ws.vm_name``). Enforce that they are the same
                # VM: a mismatched node would silently hold one VM active
                # while operating on another. Unreachable today (the
                # pending nodes always pass their own ``self._vm``), so
                # this is a loud guard on a teardown-wiring bug, not a
                # runtime branch we expect to take.
                if vm_node.row.name != ws.vm_name:
                    raise StateError(
                        f"nested teardown of workspace '{name}' was "
                        f"handed a VM node for '{vm_node.row.name}', but "
                        f"the workspace is on '{ws.vm_name}'; the node "
                        f"handed to a teardown must be the entity's own "
                        f"VM node (teardown-wiring bug).",
                        entity_kind="workspace",
                        entity_name=name,
                    )
                _keepalive_stack.enter_context(vm_node.hold_active())

        if vm is not None and vm.tailscale_host is not None:
            from agentworks.db import SessionStatus
            from agentworks.sessions.manager import (
                check_session_status,
                ensure_pids_batch,
            )
            from agentworks.sessions.tmux import force_kill_tmux_server, kill_session
            from agentworks.transports import transport

            target = transport(vm, config, logger=ssh_logger)
            sessions = db.list_sessions(workspace_name=name)
            sessions = ensure_pids_batch(sessions, db=db, config=config)
            # Snapshot console memberships before the FK cascade clears them.
            console_pairs = [(c.name, s.name) for s in sessions for c in db.list_consoles_for_session(s.name)]
            unstoppable: list[str] = []
            for session in sessions:
                status = check_session_status(session, target=target)
                if status == SessionStatus.OK:
                    if not kill_session(session.name, run_command=target.run, socket_path=session.socket_path):
                        # Race: session may have exited between check and kill. Recheck.
                        recheck = check_session_status(session, target=target)
                        if recheck != SessionStatus.STOPPED:
                            unstoppable.append(session.name)
                            continue
                elif status == SessionStatus.BROKEN:
                    if (
                        session.pid
                        and session.pid > 0
                        and force_kill_tmux_server(
                            session.pid,
                            target=target,
                            socket_path=session.socket_path,
                        )
                    ):
                        pass  # killed successfully
                    else:
                        unstoppable.append(session.name)
                elif status == SessionStatus.UNKNOWN:
                    unstoppable.append(session.name)
            if unstoppable:
                raise StateError(
                    f"cannot delete workspace '{name}': {len(unstoppable)} session(s) could not be stopped "
                    f"({', '.join(unstoppable)}).",
                    entity_kind="workspace",
                    entity_name=name,
                    hint="Resolve the stuck sessions manually before retrying.",
                )
        db.delete_sessions_for_workspace(name)

        # Best-effort: take down dangling 'Waiting for session...' windows in any
        # console that listed one of these sessions. Skips when we have no live
        # target (VM down or never had a tailnet host).
        if target is not None and console_pairs:
            from agentworks.sessions.multi_console import kill_session_windows

            kill_session_windows(target, pairs=console_pairs)

        # Revoke agent workspace grants (agents are VM-scoped, not deleted with workspaces)
        if vm is not None:
            from agentworks.agents.grants import revoke_workspace_grants

            revoke_workspace_grants(db, config, name, vm)

        if vm is not None:
            from agentworks.workspaces.backends.vm import delete_vm_workspace

            delete_vm_workspace(vm, config, name, ws.workspace_path, logger=ssh_logger)

        ssh_logger.close()

        # Remove .code-workspace file
        vscode_path = config.paths.vscode_workspaces / f"{name}.code-workspace"
        vscode_path.unlink(missing_ok=True)

        db.delete_workspace(name)
        output.info(f"Workspace '{name}' deleted")
