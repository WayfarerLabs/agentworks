"""VM workspace backend -- operations via SSH to a VM."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import typer

from agentworks.ssh import SSHTarget, ssh_target_for_vm
from agentworks.workspaces import TMUXINATOR_TEMPLATE

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
    """Create a workspace on a VM. Returns the remote workspace path."""
    from agentworks.ssh import run as ssh_run

    assert vm.tailscale_host is not None
    target = ssh_target_for_vm(vm, config)

    workspace_path = f"/home/{vm.vm_user}/workspaces/{ws_name}"

    # Create directory
    ssh_run(target, f"mkdir -p {workspace_path}")

    # Git clone if repo is set
    if template.repo:
        typer.echo(f"Cloning {template.repo}...")
        try:
            ssh_run(target, f"git clone {template.repo} {workspace_path}")
        except Exception:
            if template.repo.startswith("https://"):
                typer.echo(
                    "Hint: HTTPS repo URLs require credentials on the VM. "
                    "For private repos, use an SSH URL (git@...) so the "
                    "VM's registered SSH key provides authentication.",
                    err=True,
                )
            raise

    # Tmuxinator config
    if template.tmuxinator:
        tmux_config = TMUXINATOR_TEMPLATE.format(name=ws_name, workspace_path=workspace_path)
        ssh_run(target, f"mkdir -p {workspace_path}/.tmuxinator")
        ssh_run(
            target,
            f"cat > {workspace_path}/.tmuxinator.yml << 'TMUX_EOF'\n{tmux_config}TMUX_EOF",
        )
        # Symlink for tmuxinator to find it
        ssh_run(target, "mkdir -p ~/.config/tmuxinator")
        ssh_run(
            target,
            f"ln -sf {workspace_path}/.tmuxinator.yml ~/.config/tmuxinator/{ws_name}.yml",
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
    ssh_cmd.append(f"{vm.vm_user}@{vm.tailscale_host}")

    if use_tmuxinator and tmuxinator_enabled:
        ssh_cmd.extend(["-t", f"tmuxinator start {ws_name}"])
    else:
        ssh_cmd.extend(["-t", f"cd {workspace_path} && exec $SHELL -l"])

    os.execvp("ssh", ssh_cmd)


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
        ssh_run(target, f"rm -rf {workspace_path}")
        ssh_run(target, f"rm -f ~/.config/tmuxinator/{ws_name}.yml", check=False)
    except SSHError as e:
        typer.echo(f"Warning: remote cleanup failed: {e}", err=True)


def generate_code_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    workspace_path: str,
) -> str:
    """Generate a .code-workspace file for VS Code SSH Remote."""
    assert vm.tailscale_host is not None

    ws_file = {
        "folders": [
            {
                "uri": f"vscode-remote://ssh-remote+{vm.vm_user}@{vm.tailscale_host}{workspace_path}",
                "name": ws_name,
            }
        ],
        "settings": {},
    }

    code_ws_dir = config.paths.code_workspaces
    code_ws_dir.mkdir(parents=True, exist_ok=True)
    code_ws_path = code_ws_dir / f"{ws_name}.code-workspace"
    code_ws_path.write_text(json.dumps(ws_file, indent=2) + "\n")

    return str(code_ws_path)
