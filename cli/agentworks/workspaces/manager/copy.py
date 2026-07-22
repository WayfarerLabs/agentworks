"""Workspace copying (packing a source workspace and unpacking it to a new one)."""

from __future__ import annotations

import shlex
import subprocess
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.config import validate_name
from agentworks.errors import AlreadyExistsError, ExternalError, NotFoundError, StateError
from agentworks.vms.manager import gated_vm_boundary
from agentworks.workspaces.manager._common import _guard_vm_status, _resolve_vm, _workspace_scope

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database


def copy_workspace(
    db: Database,
    config: Config,
    source_name: str,
    *,
    dest_name: str,
    vm_name: str | None = None,
) -> None:
    """Copy a workspace to a new VM workspace.

    Orchestrated (``vms.manager.gated_vm_boundary``, WORKSPACE scope),
    the first two-VM command: the composition stays SEQUENTIAL per VM,
    exactly the imperative shape, rather than a coalesced multi-root
    single-boundary graph. The imperative command ran two separate
    binds (two prompt sessions, one per site, when source and dest
    differ), and the dest VM is only known mid-command
    (``_resolve_vm`` may interactively prompt); coalescing would merge
    prompt sessions AND hoist the interactive chooser, both behavior
    changes beyond parity. The source boundary (source workspace's
    scope) is entered on the ExitStack before the pack; when the dest
    VM differs, a SECOND boundary (dest workspace's scope) nests on
    the same stack so both VMs stay held; the same-VM case reuses the
    source composition with no second boundary. The multi-root walk
    stays available for the batch commands that already coalesce.
    """
    import contextlib
    import tempfile
    from pathlib import Path

    from agentworks.agents.grants import workspace_group
    from agentworks.bootstrap import build_registry
    from agentworks.ssh import SSHLogger
    from agentworks.transports import SSHTransport, transport

    validate_name(dest_name)

    src_ws = db.get_workspace(source_name)
    if src_ws is None:
        raise NotFoundError(
            f"workspace '{source_name}' not found",
            entity_kind="workspace",
            entity_name=source_name,
        )

    if db.get_workspace(dest_name) is not None:
        raise AlreadyExistsError(
            f"workspace '{dest_name}' already exists",
            entity_kind="workspace",
            entity_name=dest_name,
        )

    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with contextlib.ExitStack() as _keepalive_stack:
            # --- Pack from source ---
            src_vm = db.get_vm(src_ws.vm_name)
            if src_vm is None:
                raise NotFoundError(
                    f"VM '{src_ws.vm_name}' not found",
                    entity_kind="vm",
                    entity_name=src_ws.vm_name,
                )
            _guard_vm_status(src_vm)
            registry = build_registry(config)
            _keepalive_stack.enter_context(
                gated_vm_boundary(
                    db,
                    config,
                    registry,
                    src_vm,
                    scope=_workspace_scope(db, src_vm, source_name),
                )
            )
            if src_vm.tailscale_host is None:
                raise StateError(
                    f"VM '{src_vm.name}' has no Tailscale address",
                    entity_kind="vm",
                    entity_name=src_vm.name,
                )

            src_exec = transport(src_vm, config)
            # transport() returns SSHTransport for Tailscale-backed VMs; this
            # path streams scp/tar over the SSH channel and needs the raw argv.
            assert isinstance(src_exec, SSHTransport)
            output.info(f"Packing workspace '{source_name}' from VM '{src_vm.name}'...")

            # Stream tar from VM to local temp file
            ssh_args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
            if src_exec.identity_file is not None:
                ssh_args.extend(["-i", str(src_exec.identity_file)])
            ssh_args.append(f"{src_exec.user}@{src_exec.host}")
            ssh_args.append(f"tar czf - -C {src_ws.workspace_path} .")

            with open(tmp_path, "wb") as f:
                proc = subprocess.run(ssh_args, stdout=f, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                stderr = proc.stderr.decode() if proc.stderr else ""
                raise ExternalError(
                    f"pack failed: {stderr.strip()}",
                    entity_kind="workspace",
                    entity_name=source_name,
                )

            # --- Unpack to destination VM ---
            dest_vm = _resolve_vm(db, vm_name)
            _guard_vm_status(dest_vm)
            if dest_vm.name != src_vm.name:
                _keepalive_stack.enter_context(
                    gated_vm_boundary(
                        db,
                        config,
                        registry,
                        dest_vm,
                        scope=_workspace_scope(db, dest_vm, dest_name),
                    )
                )
            # Same VM: the source boundary and held span above already
            # gate and hold it; a second boundary would re-run the
            # resolve pass.
            if dest_vm.tailscale_host is None:
                raise StateError(
                    f"VM '{dest_vm.name}' has no Tailscale address",
                    entity_kind="vm",
                    entity_name=dest_vm.name,
                )

            lg = SSHLogger(dest_vm.name, "workspace-copy")
            dest_target = transport(dest_vm, config, logger=lg)

            workspace_path = f"{config.paths.vm_workspaces}/{dest_name}"
            ws_group = workspace_group(dest_name)

            output.info(f"Unpacking to workspace '{dest_name}' on VM '{dest_vm.name}'...")

            # Set up group, directory, and permissions (same as create_vm_workspace)
            dest_target.run(
                f"sh -c 'getent group {ws_group} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_group}'",
                sudo=True,
            )
            dest_target.run(f"usermod -aG {ws_group} {dest_vm.admin_username}", sudo=True)
            dest_target.run(f"mkdir -p {workspace_path}", sudo=True, timeout=10)
            dest_target.run(f"chown {dest_vm.admin_username}:{ws_group} {workspace_path}", sudo=True)
            dest_target.run(f"chmod 2770 {workspace_path}", sudo=True)
            dest_target.run(f"setfacl -d -m g::rwx -m m::rwx {workspace_path}", sudo=True)

            # Unpack archive and fix ownership
            remote_tmp = f"/tmp/{dest_name}-copy.tgz"
            dest_target.copy_to(tmp_path, remote_tmp, timeout=300)
            dest_target.run(f"tar xzf {remote_tmp} -C {workspace_path}", sudo=True, timeout=120)
            dest_target.run(f"rm -f {remote_tmp}", check=False, timeout=10)
            dest_target.run(
                f"chown -R {dest_vm.admin_username}:{ws_group} {workspace_path}",
                sudo=True,
                timeout=60,
            )
            dest_target.run(
                f"find {shlex.quote(workspace_path)} -type d -exec chmod g+s {{}} +",
                sudo=True,
                timeout=120,
            )

            db.insert_workspace(
                dest_name,
                vm_name=dest_vm.name,
                workspace_path=workspace_path,
                template="copied",
                linux_group=ws_group,
            )

            # Generate tmuxinator config and VS Code workspace
            from agentworks.workspaces.backends.vm import generate_vscode_workspace
            from agentworks.workspaces.tmuxinator import console_session_name, generate_config

            tmux_config = generate_config(dest_name, workspace_path)
            dest_target.write_file(f"{workspace_path}/.tmuxinator.yml", tmux_config)
            session = console_session_name(dest_name)
            dest_target.run("mkdir -p ~/.config/tmuxinator", timeout=10)
            dest_target.run(
                f"ln -sf {workspace_path}/.tmuxinator.yml ~/.config/tmuxinator/{session}.yml",
                timeout=10,
            )
            vscode_path = generate_vscode_workspace(dest_vm, config, dest_name, workspace_path)
            output.detail(f"VS Code workspace: {vscode_path}")
            lg.close()
    finally:
        tmp_path.unlink(missing_ok=True)

    output.result(f"Workspace '{source_name}' copied to '{dest_name}'")
