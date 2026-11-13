"""Workspace rehoming (moving a workspace to a new directory path)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import (
    AgentworksError,
    ExternalError,
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.execution.requests import Command
from agentworks.linux_users import managed_home
from agentworks.path_rendering import format_host_path
from agentworks.secrets.policy import require_exact_tty_interaction_policy
from agentworks.vms.manager import gated_vm_boundary
from agentworks.workspaces.guest import FIND_DEADLINE, PROBE_DEADLINE, REQUEST_FAILURES, open_parent_traversal, root
from agentworks.workspaces.manager._common import _guard_vm_status, _workspace_scope
from agentworks.workspaces.manager.repair import _rehome_partial_state_hint
from agentworks.workspaces.tmuxinator import generate_config, publish_config, remove_config

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, WorkspaceRow
    from agentworks.execution.access import VMAccess
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.secrets.policy import TtyInteractionPolicy


COPY_DEADLINE = 600
"""Seconds the copy of the tree to its new path may take."""

REMOVE_OLD_DEADLINE = 60
"""Seconds the removal of the old directory may take."""


def rehome_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    target_path: str | None = None,
    remove_old: bool = False,
    yes: bool = False,
    interaction: TtyInteractionPolicy,
) -> None:
    """Move a workspace to a new directory path.

    ``interaction`` is checked first, ahead of the session-status guard:
    the guard opens SSH transports and repairs stored PIDs, and the
    boundary that would otherwise reject the policy is past all of it, so
    a rejected rehome would already have touched the VM and the rows.
    """
    require_exact_tty_interaction_policy(interaction)
    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )

    # Determine target path
    from agentworks.workspaces.backends.vm import default_workspace_path

    new_path = target_path if target_path is not None else default_workspace_path(config, name)

    old_path = ws.workspace_path

    if old_path == new_path:
        output.info(f"Workspace '{name}' is already at {new_path}")
        return

    # Safety: detect overlapping paths
    old_norm = old_path.rstrip("/") + "/"
    new_norm = new_path.rstrip("/") + "/"
    if new_norm.startswith(old_norm) or old_norm.startswith(new_norm):
        raise ValidationError(
            "source and target paths overlap",
            entity_kind="workspace",
            entity_name=name,
        )

    from agentworks.harness_setup.locking import native_mutation_guard

    with native_mutation_guard(db.path, ws.vm_name) as guard:
        # Block unless all sessions are STOPPED
        from agentworks.db import PID_STOPPED, SessionStatus
        from agentworks.sessions.manager import ensure_pids_batch, observe_session_statuses

        sessions = db.list_sessions(workspace_name=name)
        if sessions:
            try:
                sessions = ensure_pids_batch(sessions, db=db, config=config)
                status_map = observe_session_statuses(sessions, db=db, config=config)
            except Exception as exc:
                raise ExternalError(
                    f"cannot verify session status for workspace '{name}' (VM may be unreachable): {exc}",
                    entity_kind="workspace",
                    entity_name=name,
                ) from exc
            not_stopped = [
                s
                for s in sessions
                if s.pid != PID_STOPPED and status_map.get(s.name, SessionStatus.UNKNOWN) != SessionStatus.STOPPED
            ]
            if not_stopped:
                names = ", ".join(s.name for s in not_stopped)
                raise StateError(
                    f"workspace '{name}' has {len(not_stopped)} non-stopped session(s) ({names}).",
                    entity_kind="workspace",
                    entity_name=name,
                    hint="Stop or delete the listed sessions first.",
                )

        _rehome_vm(
            db,
            config,
            ws,
            new_path,
            remove_old=remove_old,
            yes=yes,
            interaction=interaction,
            held_native_guard=guard,
        )


def _rehome_vm(
    db: Database,
    config: Config,
    ws: WorkspaceRow,
    new_path: str,
    *,
    remove_old: bool,
    yes: bool,
    interaction: TtyInteractionPolicy,
    held_native_guard: NativeMutationGuard,
) -> None:
    """Rehome a VM workspace.

    Orchestrated (``vms.manager.gated_vm_boundary``, WORKSPACE scope):
    the graph is the live VM alone, the activation gate replaces this
    command's ``keep_active``, and the whole move runs inside the
    held-active span. The not-found check and the VM-status guard stay
    pre-boundary; the source / target directory existence checks and
    the confirm prompt stay INSIDE the span exactly where they were:
    the checks need SSH (inherently post-gate) and the confirm renders
    their results, so they cannot move earlier without changing what
    the operator confirms.
    """

    from agentworks.bootstrap import load_request_registry
    from agentworks.vms.manager import operation_log
    from agentworks.workspaces.acls import apply_workspace_acls
    from agentworks.workspaces.backends.vm import admin_access, generate_vscode_workspace

    ws_name = ws.name
    old_path = ws.workspace_path
    vm_name = ws.vm_name

    vm = db.get_vm(vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{vm_name}' not found",
            entity_kind="vm",
            entity_name=vm_name,
        )

    _guard_vm_status(vm)
    registry = load_request_registry(config, live_database=db)
    from agentworks.workspaces.templates import resolve_live_tmuxinator

    tmuxinator_enabled = resolve_live_tmuxinator(db, registry, ws_name, ws.template)
    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        scope=_workspace_scope(db, vm, ws_name),
        interaction=interaction,
        held_native_guard=held_native_guard,
    ):
        probe = admin_access(vm, config, None)

        if not _is_directory(probe, old_path):
            raise StateError(
                f"source directory {old_path} does not exist on VM",
                entity_kind="workspace",
                entity_name=ws_name,
            )
        if _is_directory(probe, new_path):
            raise StateError(
                f"target directory {new_path} already exists on VM",
                entity_kind="workspace",
                entity_name=ws_name,
            )

        if not yes:
            output.info(f"Rehome workspace '{ws_name}':")
            output.detail(f"From: {old_path}")
            output.detail(f"To:   {new_path}")
            if remove_old:
                output.detail("Old directory will be REMOVED after copy")
            else:
                output.detail("Old directory will be LEFT IN PLACE")
            if not output.confirm("Proceed?"):
                raise UserAbort("rehome cancelled")

        # The log closes exactly once, however the move ends.
        with operation_log(vm.name, "workspace-rehome") as ssh_logger:
            admin = admin_access(vm, config, ssh_logger)
            try:
                # Create target directory as root and chown to admin so rsync can write
                admin.run(root("mkdir", "-p", "--", new_path), check=True)
                admin.run(root("chown", vm.admin_username, new_path), check=True)

                # Copy with rsync (fall back to cp -a). The trailing slash
                # matters for rsync: the contents of the source into the target.
                output.info("Copying workspace...")
                has_rsync = admin.run(Command(("which", "rsync"), deadline=PROBE_DEADLINE))
                if has_rsync.require_completed(vm.name) == 0:
                    admin.run(
                        Command(("rsync", "-a", "--", f"{old_path}/", f"{new_path}/"), deadline=COPY_DEADLINE),
                        check=True,
                    )
                else:
                    admin.run(
                        root("cp", "-a", "--", f"{old_path}/.", f"{new_path}/", deadline=COPY_DEADLINE), check=True
                    )

                if not _is_directory(admin, new_path):
                    raise ExternalError(
                        "copy verification failed, target directory not found",
                        entity_kind="workspace",
                        entity_name=ws_name,
                        hint=f"SSH log: {ssh_logger.display_path}",
                    )

                # Fix ownership, permissions, and ACLs on the new path
                output.info("Setting permissions...")
                admin.run(root("chown", f"{vm.admin_username}:{ws.linux_group}", new_path), check=True)
                admin.run(root("chmod", "2770", new_path), check=True)
                admin.run(
                    root("find", new_path, "-type", "d", "-exec", "chmod", "g+s", "{}", "+", deadline=FIND_DEADLINE),
                    check=True,
                )
                # Canonical workspace ACL, the same shared spec workspace create
                # and repair apply, so a rehomed workspace lands in the canonical
                # state (its first repair is an ACL no-op). Per #254 this now also
                # denies `other` on both the access and default ACLs, which the
                # old hand-rolled block here did not. ACL failure stays non-fatal
                # (warn and continue, exactly as before): the rsync copy already
                # succeeded, so a later repair converges the tree.
                try:
                    apply_workspace_acls(admin.files(), new_path)
                except REQUEST_FAILURES as e:
                    output.warn(f"ACL setup failed: {e}")

                # Only true ancestors of the new path get a+x; its own mode is
                # the canonical 2770 set above.
                admin.run(open_parent_traversal(new_path), check=True)

                # Regenerate the tmuxinator config at the new path, replacing the
                # link to the old one.
                home = managed_home(vm.admin_username)
                if tmuxinator_enabled:
                    publish_config(
                        admin,
                        home=home,
                        ws_name=ws_name,
                        workspace_path=new_path,
                        text=generate_config(ws_name, new_path),
                        replace_link=True,
                    )
                else:
                    remove_config(admin, home=home, ws_name=ws_name, workspace_path=new_path)

                # Retire old-location and copied Git pairs while the old row
                # still authorizes their source, then establish the new pairs.
                from agentworks.harness_setup.cleanup import rehome_workspace_artifacts

                rehome_workspace_artifacts(db, ws, new_path, admin, redactions=ssh_logger.redactions)

                # Update database
                db.update_workspace_path(ws_name, new_path)
                output.detail(f"Database updated: workspace_path = {new_path}")

                # Regenerate VS Code workspace file
                vscode_path = generate_vscode_workspace(vm, config, ws_name, new_path)
                output.detail(f"VS Code workspace updated: {format_host_path(vscode_path)}")

                # Handle old directory
                if remove_old:
                    output.info(f"Removing old directory {old_path}...")
                    admin.run(root("rm", "-rf", "--", old_path, deadline=REMOVE_OLD_DEADLINE), check=True)
                    output.detail("Old directory removed")
                else:
                    output.info(f"Old directory left in place at {old_path}")
                    output.detail("Remove it manually when ready, or re-run with --remove-old")

            except KeyboardInterrupt:
                output.warn(
                    f"Cancelling workspace rehome '{ws_name}'. "
                    f"{_rehome_partial_state_hint(db, ws_name, old_path, new_path)} "
                    f"SSH log: {ssh_logger.display_path}"
                )
                raise
            except AgentworksError:
                raise
            except Exception as e:
                raise ExternalError(
                    f"during rehome: {e}",
                    entity_kind="workspace",
                    entity_name=ws_name,
                    hint=(
                        f"SSH log: {ssh_logger.display_path}. "
                        f"{_rehome_partial_state_hint(db, ws_name, old_path, new_path)}"
                    ),
                ) from e

        output.result(f"Workspace '{ws_name}' rehomed to {new_path}")


def _is_directory(access: VMAccess, path: str) -> bool:
    """Whether a directory is at ``path``, as the admin sees it; an outcome that is not a completion raises."""
    return access.run(Command(("test", "-d", path), deadline=PROBE_DEADLINE)).require_completed(access.vm_name) == 0
