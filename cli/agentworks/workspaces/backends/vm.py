"""VM workspace backend -- operations via SSH to a VM."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import typer

from agentworks.ssh import ssh_target_for_vm
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

    Idempotent: if the workspace directory already exists (e.g. from a
    previous interrupted attempt), it is removed and recreated.
    """
    from agentworks.ssh import run as ssh_run
    from agentworks.ssh import run_as_root

    assert vm.tailscale_host is not None
    target = ssh_target_for_vm(vm, config)
    lg = logger

    workspace_path = f"/home/{vm.admin_username}/workspaces/{ws_name}"
    ws_group = f"ws-{ws_name}"

    # Remove stale directory from a previous interrupted attempt
    exists = ssh_run(target, f"test -d {workspace_path}", check=False, timeout=10, logger=lg)
    if exists.ok:
        typer.echo("  Removing stale workspace directory from previous attempt...")
        ssh_run(target, f"rm -rf {workspace_path}", timeout=30, logger=lg)

    # Create workspace group (idempotent), add admin, and set up directory with setgid
    run_as_root(target, f"sh -c 'getent group {ws_group} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_group}'", logger=lg)
    run_as_root(target, f"usermod -aG {ws_group} {vm.admin_username}", logger=lg)
    ssh_run(target, f"mkdir -p {workspace_path}", timeout=10, logger=lg)
    run_as_root(target, f"chown {vm.admin_username}:{ws_group} {workspace_path}", logger=lg)
    run_as_root(target, f"chmod 2775 {workspace_path}", logger=lg)

    # Git clone if repo is set
    if template.repo:
        typer.echo(f"Cloning {template.repo}...")
        try:
            ssh_run(target, f"git clone {template.repo} {workspace_path}", timeout=300, logger=lg)
            # Ensure cloned files inherit the workspace group
            run_as_root(target, f"chgrp -R {ws_group} {workspace_path}", logger=lg)
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

    # Tmuxinator config (no tasks yet at workspace creation time)
    if template.tmuxinator:
        from agentworks.ssh import write_file

        tmux_config = generate_config(ws_name, workspace_path)
        write_file(target, f"{workspace_path}/.tmuxinator.yml", tmux_config, logger=lg)
        # Symlink so tmuxinator can find it by console session name
        session = console_session_name(ws_name)
        ssh_run(target, "mkdir -p ~/.config/tmuxinator", timeout=10, logger=lg)
        ssh_run(
            target,
            f"ln -sf {workspace_path}/.tmuxinator.yml ~/.config/tmuxinator/{session}.yml",
            timeout=10,
            logger=lg,
        )

    return workspace_path


def shell_vm_workspace(
    vm: VMRow,
    config: Config,
    workspace_path: str,
) -> None:
    """Open a plain shell into a VM workspace."""
    from agentworks.ssh import interactive, ssh_target_for_vm

    target = ssh_target_for_vm(vm, config)
    sys.exit(interactive(target, f"cd {workspace_path} && exec $SHELL -l"))


def console_vm_workspace(
    vm: VMRow,
    config: Config,
    ws_name: str,
    *,
    recreate: bool = False,
) -> None:
    """Open the workspace console (tmuxinator) on a VM."""
    from agentworks.ssh import interactive, run, ssh_target_for_vm

    session = console_session_name(ws_name)
    target = ssh_target_for_vm(vm, config)

    if recreate:
        run(target, f"tmux kill-session -t {session}", check=False, timeout=10)

    sys.exit(interactive(target, f"tmuxinator start {session}"))


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
    from agentworks.ssh import run as ssh_run

    assert vm.tailscale_host is not None
    target = ssh_target_for_vm(vm, config)
    lg = logger

    try:
        ssh_run(target, f"rm -rf {workspace_path}", timeout=30, logger=lg)
        session = console_session_name(ws_name)
        ssh_run(target, f"rm -f ~/.config/tmuxinator/{session}.yml", check=False, timeout=10, logger=lg)
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
