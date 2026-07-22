"""Workspace rehoming (moving a workspace to a new directory path)."""

from __future__ import annotations

import shlex
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
from agentworks.vms.manager import gated_vm_boundary
from agentworks.workspaces.manager._common import _guard_vm_status, _workspace_scope
from agentworks.workspaces.manager.reinit import _rehome_partial_state_hint

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, WorkspaceRow


def rehome_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    target_path: str | None = None,
    remove_old: bool = False,
    yes: bool = False,
) -> None:
    """Move a workspace to a new directory path."""
    ws = db.get_workspace(name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{name}' not found",
            entity_kind="workspace",
            entity_name=name,
        )

    # Determine target path
    new_path = target_path if target_path is not None else f"{config.paths.vm_workspaces}/{name}"

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

    # Block unless all sessions are STOPPED
    from agentworks.db import PID_STOPPED, SessionStatus
    from agentworks.sessions.manager import batch_check_all_sessions, ensure_pids_batch

    sessions = db.list_sessions(workspace_name=name)
    if sessions:
        try:
            sessions = ensure_pids_batch(sessions, db=db, config=config)
            status_map = batch_check_all_sessions(sessions, db=db, config=config)
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

    _rehome_vm(db, config, ws, new_path, remove_old=remove_old, yes=yes)


def _rehome_vm(
    db: Database,
    config: Config,
    ws: WorkspaceRow,
    new_path: str,
    *,
    remove_old: bool,
    yes: bool,
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

    from agentworks.bootstrap import build_registry
    from agentworks.ssh import SSHError, SSHLogger
    from agentworks.transports import transport
    from agentworks.workspaces.backends.vm import generate_vscode_workspace

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
    registry = build_registry(config)
    with gated_vm_boundary(db, config, registry, vm, scope=_workspace_scope(db, vm, ws_name)):
        target = transport(vm, config)

        # Verify source exists
        src_check = target.run(f"test -d {old_path}", check=False, timeout=10)
        if not src_check.ok:
            raise StateError(
                f"source directory {old_path} does not exist on VM",
                entity_kind="workspace",
                entity_name=ws_name,
            )

        # Verify target does not exist
        dst_check = target.run(f"test -d {new_path}", check=False, timeout=10)
        if dst_check.ok:
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

        ssh_logger = SSHLogger(vm.name, "workspace-rehome")
        target = transport(vm, config, logger=ssh_logger)
        ws_group = ws.linux_group

        # Shell-quote paths once up front; both are interpolated into many shell
        # commands below. Without this, any space or shell-special character in
        # a workspace path breaks the command (and exposes an injection surface
        # if a path is ever supplied by an attacker-controlled source).
        np = shlex.quote(new_path)
        op = shlex.quote(old_path)

        # Outer try/finally ensures the SSH logger is closed exactly once. Earlier
        # versions called close() in every except branch AND on the success path,
        # which double-wrote the "Finished" footer when an inner raise re-entered
        # an outer except.
        try:
            try:
                # Create target directory as root and chown to admin so rsync can write
                target.run(f"mkdir -p {np}", sudo=True)
                target.run(f"chown {vm.admin_username} {np}", sudo=True)

                # Copy with rsync (fall back to cp -a). Trailing slash matters for
                # rsync semantics ("contents of source into target"); putting it
                # AFTER the quoted path works because adjacent quoted/unquoted
                # tokens concatenate in shell.
                output.info("Copying workspace...")
                has_rsync = target.run("which rsync", check=False, timeout=10)
                if has_rsync.ok:
                    target.run(f"rsync -a {op}/ {np}/", timeout=600)
                else:
                    target.run(f"cp -a {op}/. {np}/", sudo=True, timeout=600)

                # Verify copy succeeded
                verify = target.run(f"test -d {np}", check=False, timeout=10)
                if not verify.ok:
                    raise ExternalError(
                        "copy verification failed, target directory not found",
                        entity_kind="workspace",
                        entity_name=ws_name,
                        hint=f"SSH log: {ssh_logger.path}",
                    )

                # Fix ownership, permissions, and ACLs on the new path
                output.info("Setting permissions...")
                target.run(f"chown {vm.admin_username}:{ws_group} {np}", sudo=True)
                target.run(f"chmod 2770 {np}", sudo=True)
                target.run(f"find {np} -type d -exec chmod g+s {{}} +", sudo=True, timeout=120)
                try:
                    target.run(
                        f"find {np} -type d -exec setfacl -d -m g::rwx -m m::rwx {{}} +",
                        sudo=True,
                        timeout=120,
                    )
                    target.run(
                        f"setfacl -R -m g::rwx -m m::rwx {np}",
                        sudo=True,
                        timeout=120,
                    )
                except SSHError as e:
                    output.warn(f"ACL setup failed: {e}")

                # Fix parent directory traversal. sudo=True already wraps the
                # command in `sudo -n bash -c '<quoted>'`, so the script runs in
                # a single bash context. No extra `sh -c '...'` indirection is
                # needed (and the explicit wrapper made path quoting impossible
                # to do safely).
                target.run(
                    f'p={np}; while [ "$p" != "/" ]; do chmod a+x "$p"; p=$(dirname "$p"); done',
                    sudo=True,
                )

                # Regenerate tmuxinator config at new path. write_file passes
                # remote_path to scp as a subprocess arg (not interpolated into a
                # local shell), so f-string concatenation is safe on the client
                # side. The remote scp/sftp handler may still interpret the path
                # per its own rules; if a future change funnels untrusted paths
                # through here, revisit.
                from agentworks.workspaces.tmuxinator import console_session_name, generate_config

                tmux_config = generate_config(ws_name, new_path)
                target.write_file(f"{new_path}/.tmuxinator.yml", tmux_config)
                session = console_session_name(ws_name)
                target.run("mkdir -p ~/.config/tmuxinator", timeout=10)
                # Keep ~/.config/tmuxinator/ literal so tilde expansion still
                # happens; quote just the filename for layered defense.
                target.run(
                    f"ln -sf {np}/.tmuxinator.yml ~/.config/tmuxinator/{shlex.quote(session)}.yml",
                    timeout=10,
                )

                # Update database
                db.update_workspace_path(ws_name, new_path)
                output.detail(f"Database updated: workspace_path = {new_path}")

                # Regenerate VS Code workspace file
                vscode_path = generate_vscode_workspace(vm, config, ws_name, new_path)
                output.detail(f"VS Code workspace updated: {vscode_path}")

                # Handle old directory
                if remove_old:
                    output.info(f"Removing old directory {old_path}...")
                    target.run(f"rm -rf {op}", sudo=True, timeout=60)
                    output.detail("Old directory removed")
                else:
                    output.info(f"Old directory left in place at {old_path}")
                    output.detail("Remove it manually when ready, or re-run with --remove-old")

            except KeyboardInterrupt:
                output.warn(
                    f"Cancelling workspace rehome '{ws_name}'. "
                    f"{_rehome_partial_state_hint(db, ws_name, old_path, new_path)} "
                    f"SSH log: {ssh_logger.path}"
                )
                raise
            except AgentworksError:
                raise
            except Exception as e:
                raise ExternalError(
                    f"during rehome: {e}",
                    entity_kind="workspace",
                    entity_name=ws_name,
                    hint=(f"SSH log: {ssh_logger.path}. {_rehome_partial_state_hint(db, ws_name, old_path, new_path)}"),
                ) from e
        finally:
            ssh_logger.close()

        output.result(f"Workspace '{ws_name}' rehomed to {new_path}")
