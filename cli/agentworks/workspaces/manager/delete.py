"""Workspace deletion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import SessionMode
from agentworks.errors import AgentworksError, NotFoundError, StateError, UserAbort, ValidationError
from agentworks.vms.home_layout import AGENTWORKS_ARTIFACTS
from agentworks.vms.manager import gated_vm_boundary, operation_log
from agentworks.workspaces.guest import stored_workspace_path
from agentworks.workspaces.manager._common import _workspace_scope

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, WorkspaceRow
    from agentworks.execution.access import VMAccess
    from agentworks.native_locks import LockRequest
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.vms.nodes import LiveVMNode


def delete_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    force: bool = False,
    yes: bool = False,
    vm_node: LiveVMNode | None = None,
    interaction: TtyInteractionPolicy,
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
    # A stored path that names no one directory (a '~' row the state migration
    # left relative) is never acted on: the row, its sessions, grants and group
    # still go, so the name can be reused, and the VM's files are left for the
    # operator to check. The removal takes the normalized path, never the raw
    # spelling: a stored trailing slash would make ``rm`` follow a final link
    # and empty its target instead of removing the link.
    try:
        stored_path: str | None = stored_workspace_path(ws.workspace_path)
    except ValidationError:
        stored_path = None
    path_known = stored_path is not None

    # Check for sessions
    session_names = [s.name for s in db.list_sessions(workspace_name=name)]
    session_count = len(session_names)
    if session_count > 0 and not force:
        raise StateError(
            f"workspace '{name}' has {session_count} session(s).",
            entity_kind="workspace",
            entity_name=name,
            hint="Delete the sessions first, or pass --force to also delete them.",
        )

    if not path_known:
        output.warn(
            f"Workspace '{name}' has the stored path {ws.workspace_path!r}, which names no one directory on the VM, "
            "so its directory is not touched. Check the VM for it by hand."
        )

    from agentworks.native_locks import probe_locks

    request = _workspace_delete_request(db, ws)
    if vm_node is None:
        # A conflict refuses before the confirmation; a nested teardown's caller holds the set.
        probe_locks(db.path, request)

    if not yes:
        if path_known:
            msg = (
                f"Delete workspace '{name}' and its files? "
                "Uncommitted changes, untracked files, stashes and commits not pushed elsewhere will be lost."
            )
        else:
            msg = f"Delete workspace '{name}'? Its files on the VM are kept."
        if session_count > 0:
            msg += f" ({session_count} session(s) will also be deleted)"
        if not output.confirm(msg):
            raise UserAbort("delete cancelled")

    import contextlib

    from agentworks.recheck import require_request_unchanged, require_unchanged, require_vm_unchanged, workspace_fields
    from agentworks.vms.nodes import lock_through
    from agentworks.workspaces.locks import require_trees_unchanged, walk_trees, with_trees

    vm = db.get_vm(ws.vm_name)
    if vm is not None:
        # Refuse a session row the owner rule rejects before any VM work.
        from agentworks.sessions.manager import resolve_session_tmux_server

        for session in db.list_sessions(workspace_name=name):
            resolve_session_tmux_server(db, session)
    with contextlib.ExitStack() as _keepalive_stack:
        node = vm_node
        if vm is not None:
            if vm_node is None:
                # The standalone composition root: build the boundary here.
                # Its secrets resolve before the lock set below.
                from agentworks.bootstrap import load_request_registry

                registry = load_request_registry(config, live_database=db)
                node, _resolver, _ctx = _keepalive_stack.enter_context(
                    gated_vm_boundary(
                        db,
                        config,
                        registry,
                        vm,
                        scope=_workspace_scope(db, vm, name),
                        interaction=interaction,
                    )
                )
                vm = node.row
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
                vm = vm_node.row
        # Only the entry the stored path names is removed, so its walk resolves
        # every component but the last and names that entry without following it.
        trees = None
        locked = request
        if vm is not None and stored_path is not None:
            trees = walk_trees(vm, config, [stored_path], unlink_only=True)
            locked = with_trees(request, trees)
        # A workspace whose VM row is gone, outside a nested teardown, has no
        # node to lock through.
        _keepalive_stack.enter_context(lock_through(node, db.path, locked))
        # What the confirmation showed: the workspace and the sessions it counted,
        # and the lock set as its grants, sessions and consoles build it now.
        if vm is not None:
            require_vm_unchanged(db, vm)
        require_unchanged("workspace", name, workspace_fields(ws), workspace_fields(db.get_workspace(name)))
        require_unchanged("workspace", name, session_names, [s.name for s in db.list_sessions(workspace_name=name)])
        require_request_unchanged("workspace", name, request, _workspace_delete_request(db, ws))
        if vm is not None and trees is not None:
            require_trees_unchanged(vm, config, trees)
        log = _keepalive_stack.enter_context(operation_log(ws.vm_name, "workspace-delete"))
        output.info(f"Deleting workspace '{name}' on VM '{ws.vm_name}'...")
        # console_pairs is populated only when we reach the VM; the
        # post-delete cleanup is best-effort and skips when admin is None.
        admin: VMAccess | None = None
        console_pairs: list[tuple[str, str]] = []
        if vm is not None and vm.tailscale_host is not None:
            from agentworks.sessions.manager import (
                _identity_access,
                _teardown_session,
                ensure_pids_batch,
            )

            # The admin tears an agent's session down as that agent and owns the console windows.
            admin = _identity_access(vm, config, None, logger=log)
            sessions = db.list_sessions(workspace_name=name)
            sessions = ensure_pids_batch(sessions, db=db, config=config)
            # Snapshot console memberships before the FK cascade clears them.
            console_pairs = [(c.name, s.name) for s in sessions for c in db.list_consoles_for_session(s.name)]
            unstoppable: list[str] = []
            for session in sessions:
                try:
                    _teardown_session(
                        session,
                        access=admin,
                        target_owns_session=session.mode == SessionMode.ADMIN.value,
                        db=db,
                        force=True,
                        legacy_running_authorized=True,
                    )
                except Exception:
                    unstoppable.append(session.name)
            if unstoppable:
                raise StateError(
                    f"cannot delete workspace '{name}': {len(unstoppable)} session(s) could not be stopped "
                    f"({', '.join(unstoppable)}).",
                    entity_kind="workspace",
                    entity_name=name,
                    hint="Resolve the stuck sessions manually before retrying.",
                )
        if vm is not None and path_known:
            from agentworks.harness_setup.cleanup import cleanup_workspace_excludes
            from agentworks.sessions.manager import _identity_access

            cleanup_workspace_excludes(
                db,
                ws,
                lambda: _identity_access(vm, config, None, logger=log),
                redactions=log.redactions,
            )
        if vm is not None and admin is not None:
            from agentworks.artifacts.session import cleanup_session_artifacts
            from agentworks.harness_setup.state import read_native_setup
            from agentworks.sessions.manager import _session_access

            for session in db.list_sessions(workspace_name=name):
                try:
                    if not any(
                        record.artifact_files for record in read_native_setup(db, "session", session.name).records
                    ):
                        continue
                    cleanup_session_artifacts(
                        db, session, _session_access(session, vm=vm, config=config, db=db, logger=log)
                    )
                except Exception:
                    output.warn(
                        f"Private artifact cleanup for session '{session.name}' could not finish. "
                        f"Check ~/{AGENTWORKS_ARTIFACTS}/session/{session.session_uuid} in that session user's "
                        "home; remaining files will no longer have managed cleanup evidence after deletion."
                    )
        db.delete_sessions_for_workspace(name)

        # Best-effort: take down dangling 'Waiting for session...' windows in any
        # console that listed one of these sessions. Skips when the VM was
        # not reached (VM down or never had a tailnet host).
        if admin is not None and console_pairs:
            from agentworks.sessions.multi_console import kill_session_windows

            kill_session_windows(admin, pairs=console_pairs)

        # Revoke agent workspace grants (agents are VM-scoped, not deleted with
        # workspaces), then remove the workspace from the VM. A removal that
        # fails raises with the workspace's row kept, so a rerun retries it.
        if vm is not None:
            from agentworks.agents.grants import revoke_workspace_grants
            from agentworks.workspaces.backends.vm import delete_vm_workspace, delete_vm_workspace_keeping_files

            try:
                revoke_workspace_grants(db, config, name, vm)
                if stored_path is not None:
                    delete_vm_workspace(vm, config, stored_path, ws.linux_group, logger=log)
                else:
                    delete_vm_workspace_keeping_files(vm, config, ws.linux_group, logger=log)
            except AgentworksError as error:
                error.add_hint(f"The workspace's row was kept; run 'agw workspace delete {name}' again.")
                raise

        # Remove .code-workspace file
        vscode_path = config.paths.vscode_workspaces / f"{name}.code-workspace"
        vscode_path.unlink(missing_ok=True)

        db.delete_workspace(name)
        output.info(f"Workspace '{name}' deleted")


def _workspace_delete_request(db: Database, ws: WorkspaceRow) -> LockRequest:
    """Workspace delete's lock set without its tree.

    The workspace, each of its sessions, its grants (the admin's and each
    granted agent's) and the consoles holding its sessions, all exclusive.
    A workspace whose VM row is gone has no admin to name, so it locks the
    agents' grants alone.
    """
    from agentworks.native_locks import LockRequest
    from agentworks.native_locks.rows import (
        admin_account_entity,
        agent_account_entity,
        consoles_holding,
        grant_entity_for,
        session_entity_for,
        workspace_entity_for,
    )

    sessions = db.list_sessions(workspace_name=ws.name)
    vm = db.get_vm(ws.vm_name)
    accounts = [] if vm is None else [admin_account_entity(vm)]
    accounts.extend(
        agent_account_entity(agent)
        for agent in db.list_agents(vm_name=ws.vm_name)
        if ws.name in db.list_granted_workspaces(agent.name)
    )
    exclusive = (
        workspace_entity_for(ws),
        *(session_entity_for(db, session) for session in sessions),
        *(grant_entity_for(ws, account) for account in accounts),
        *consoles_holding(db, sessions),
    )
    return LockRequest(exclusive=exclusive)
