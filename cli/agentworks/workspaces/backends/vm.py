"""VM workspace backend -- operations via SSH to a VM."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import AlreadyExistsError
from agentworks.transports import transport
from agentworks.workspaces.tmuxinator import console_session_name, generate_config

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.ssh import SSHLogger
    from agentworks.workspaces.templates import ResolvedTemplate


def create_vm_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    template: ResolvedTemplate,
    *,
    logger: SSHLogger | None = None,
) -> str:
    """Create a workspace on a VM. Returns the remote workspace path.

    Errors if the workspace directory already exists on the VM.
    """
    from agentworks.agents.manager import workspace_group

    assert vm.tailscale_host is not None
    target = transport(vm, config, logger=logger)

    workspace_path = f"{config.paths.vm_workspaces}/{ws_name}"
    ws_group = workspace_group(ws_name)

    # Refuse to create if directory already exists
    exists = target.run(f"test -d {workspace_path}", check=False, timeout=10)
    if exists.ok:
        raise AlreadyExistsError(
            f"directory {workspace_path} already exists on the VM.",
            entity_kind="workspace",
            entity_name=ws_name,
            hint=(
                f"Remove it manually (ssh to the VM and 'sudo rm -rf {workspace_path}') "
                "or choose a different name."
            ),
        )

    # Create workspace group (idempotent), add admin, and set up directory with setgid
    target.run(f"sh -c 'getent group {ws_group} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_group}'", sudo=True)
    target.run(f"usermod -aG {ws_group} {vm.admin_username}", sudo=True)
    target.run(f"mkdir -p {workspace_path}", sudo=True)
    target.run(f"chown {vm.admin_username}:{ws_group} {workspace_path}", sudo=True)
    target.run(f"chmod 2770 {workspace_path}", sudo=True)
    # Set default ACLs so files created inside are group-writable
    target.run(f"setfacl -d -m g::rwx -m m::rwx {workspace_path}", sudo=True)

    # Git clone if repo is set
    if template.repo:
        output.info(f"Cloning {template.repo}...")
        try:
            target.run(f"git clone {template.repo} {workspace_path}", timeout=300)
            # Ensure cloned files inherit the workspace group and subdirectories
            # have SGID so new files (including atomic writes) get the right group
            target.run(f"chgrp -R {ws_group} {workspace_path}", sudo=True)
            import shlex

            sgid_cmd = f"find {shlex.quote(workspace_path)} -type d -exec chmod g+s {{}} +"
            target.run(sgid_cmd, sudo=True, timeout=120)
        except Exception:
            if template.repo.startswith("git@"):
                output.warn(
                    "Hint: SSH repo URLs are not supported. Use HTTPS URLs "
                    "and configure git credentials with 'vm add-git-credential'."
                )
            else:
                output.warn(
                    "Hint: for private repos, ensure git credentials are "
                    "configured on the VM (see 'vm add-git-credential')."
                )
            raise

    # Tmuxinator config (no tasks yet at workspace creation time)
    if template.tmuxinator:
        tmux_config = generate_config(ws_name, workspace_path)
        target.write_file(f"{workspace_path}/.tmuxinator.yml", tmux_config)
        # Symlink so tmuxinator can find it by console session name
        session = console_session_name(ws_name)
        target.run("mkdir -p ~/.config/tmuxinator", timeout=10)
        target.run(
            f"ln -sf {workspace_path}/.tmuxinator.yml ~/.config/tmuxinator/{session}.yml",
            timeout=10,
        )

    return workspace_path


def shell_vm_workspace(
    vm: VMRow,
    config: Config,
    workspace_path: str,
) -> None:
    """Open a plain shell into a VM workspace."""
    target = transport(vm, config)
    sys.exit(target.interactive(f"cd {workspace_path} && exec $SHELL -l"))


def console_vm_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    *,
    recreate: bool = False,
) -> None:
    """Open the workspace console (tmuxinator) on a VM."""
    session = console_session_name(ws_name)
    target = transport(vm, config)

    if recreate:
        target.run(f"tmux kill-session -t {session}", check=False, timeout=10)

    sys.exit(target.interactive(f"tmuxinator start {session}"))


def delete_vm_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    workspace_path: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Delete a workspace from a VM."""
    from agentworks.ssh import SSHError

    assert vm.tailscale_host is not None
    target = transport(vm, config, logger=logger)

    try:
        target.run(f"rm -rf {workspace_path}", sudo=True, timeout=30)
        session = console_session_name(ws_name)
        target.run(f"rm -f ~/.config/tmuxinator/{session}.yml", check=False, timeout=10)
    except SSHError as e:
        output.warn(f"remote cleanup failed: {e}")


def generate_vscode_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    workspace_path: str,
) -> str:
    """Generate a .code-workspace file for VS Code SSH Remote."""
    from agentworks.ssh_config import ssh_host_alias

    # Use the SSH config alias so VS Code picks up the right host/user/key
    ssh_host = ssh_host_alias(vm.name, config.operator.ssh_host_prefix)

    ws_file = {
        "folders": [
            {
                "uri": f"vscode-remote://ssh-remote+{ssh_host}{workspace_path}",
            }
        ],
        "remoteAuthority": f"ssh-remote+{ssh_host}",
    }

    vscode_dir = config.paths.vscode_workspaces
    vscode_dir.mkdir(parents=True, exist_ok=True)
    vscode_path = vscode_dir / f"{ws_name}.code-workspace"
    vscode_path.write_text(json.dumps(ws_file, indent=2) + "\n")

    return str(vscode_path)
