"""VM workspace backend -- operations via SSH to a VM."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import typer

from agentworks.ssh import ssh_target_for_vm
from agentworks.workspaces.tmuxinator import generate_config

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.workspaces.templates import ResolvedTemplate


def create_vm_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    template: ResolvedTemplate,
) -> str:
    """Create a workspace on a VM. Returns the remote workspace path.

    Idempotent: if the workspace directory already exists (e.g. from a
    previous interrupted attempt), it is removed and recreated.
    """
    from agentworks.ssh import run as ssh_run

    assert vm.tailscale_host is not None
    target = ssh_target_for_vm(vm, config)

    workspace_path = f"/home/{vm.admin_username}/workspaces/{ws_name}"

    # Remove stale directory from a previous interrupted attempt
    exists = ssh_run(target, f"test -d {workspace_path}", check=False, timeout=10)
    if exists.ok:
        typer.echo("  Removing stale workspace directory from previous attempt...")
        ssh_run(target, f"rm -rf {workspace_path}", timeout=30)

    # Create directory
    ssh_run(target, f"mkdir -p {workspace_path}", timeout=10)

    # Git clone if repo is set
    if template.repo:
        typer.echo(f"Cloning {template.repo}...")
        try:
            ssh_run(target, f"git clone {template.repo} {workspace_path}", timeout=300)
        except Exception:
            if template.repo.startswith("git@"):
                typer.echo(
                    "Hint: SSH repo URLs are not supported. Use HTTPS URLs "
                    "and configure git credentials with 'vm add-git-credential'.",
                    err=True,
                )
            else:
                typer.echo(
                    "Hint: for private repos, ensure git credentials are "
                    "configured on the VM (see 'vm add-git-credential').",
                    err=True,
                )
            raise

    # Tmuxinator config (no agents yet at workspace creation time)
    if template.tmuxinator:
        from agentworks.ssh import write_file

        tmux_config = generate_config(ws_name, workspace_path)
        write_file(target, f"{workspace_path}/.tmuxinator.yml", tmux_config)
        # Symlink for tmuxinator to find it
        ssh_run(target, "mkdir -p ~/.config/tmuxinator", timeout=10)
        ssh_run(
            target,
            f"ln -sf {workspace_path}/.tmuxinator.yml ~/.config/tmuxinator/{ws_name}.yml",
            timeout=10,
        )

    return workspace_path


def shell_vm_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    workspace_path: str,
    *,
    use_tmuxinator: bool = True,
    tmuxinator_enabled: bool = True,
) -> None:
    """Open a shell into a VM workspace."""
    assert vm.tailscale_host is not None

    ssh_cmd = ["ssh"]
    if config.user.ssh_private_key:
        ssh_cmd.extend(["-i", str(config.user.ssh_private_key)])
    ssh_cmd.append(f"{vm.admin_username}@{vm.tailscale_host}")

    if use_tmuxinator and tmuxinator_enabled:
        ssh_cmd.extend(["-t", f"tmuxinator start {ws_name}"])
    else:
        ssh_cmd.extend(["-t", f"cd {workspace_path} && exec $SHELL -l"])

    sys.exit(subprocess.call(ssh_cmd))


def delete_vm_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    workspace_path: str,
) -> None:
    """Delete a workspace from a VM."""
    from agentworks.ssh import SSHError
    from agentworks.ssh import run as ssh_run

    assert vm.tailscale_host is not None
    target = ssh_target_for_vm(vm, config)

    try:
        ssh_run(target, f"rm -rf {workspace_path}", timeout=30)
        ssh_run(target, f"rm -f ~/.config/tmuxinator/{ws_name}.yml", check=False, timeout=10)
    except SSHError as e:
        typer.echo(f"Warning: remote cleanup failed: {e}", err=True)


def generate_code_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    workspace_path: str,
) -> str:
    """Generate a .code-workspace file for VS Code SSH Remote."""
    from agentworks.ssh_config import ssh_host_alias

    # Use the SSH config alias so VS Code picks up the right host/user/key
    ssh_host = ssh_host_alias(vm.name, config.user.ssh_host_prefix)

    ws_file = {
        "folders": [
            {
                "uri": f"vscode-remote://ssh-remote+{ssh_host}{workspace_path}",
            }
        ],
        "remoteAuthority": f"ssh-remote+{ssh_host}",
    }

    code_ws_dir = config.paths.code_workspaces
    code_ws_dir.mkdir(parents=True, exist_ok=True)
    code_ws_path = code_ws_dir / f"{ws_name}.code-workspace"
    code_ws_path.write_text(json.dumps(ws_file, indent=2) + "\n")

    return str(code_ws_path)
