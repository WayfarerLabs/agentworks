"""Local workspace backend -- operations directly on the host filesystem."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from agentworks.workspaces import TMUXINATOR_TEMPLATE

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.workspaces.templates import ResolvedTemplate


def create_local_workspace(
    config: Config,
    ws_name: str,
    template: ResolvedTemplate,
) -> str:
    """Create a local workspace. Returns the workspace path."""
    workspace_dir = config.paths.local_workspaces / ws_name
    workspace_path = str(workspace_dir)

    if workspace_dir.exists():
        typer.echo(f"Error: directory already exists: {workspace_path}", err=True)
        raise typer.Exit(1)

    # Git clone or just create directory
    if template.repo:
        typer.echo(f"Cloning {template.repo}...")
        result = subprocess.run(
            ["git", "clone", template.repo, workspace_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            typer.echo(f"Error: git clone failed: {result.stderr.strip()}", err=True)
            if template.repo.startswith("https://"):
                typer.echo(
                    "Hint: HTTPS repo URLs require credentials. "
                    "For private repos, use an SSH URL (git@...) so "
                    "your SSH key provides authentication.",
                    err=True,
                )
            raise typer.Exit(1)
    else:
        workspace_dir.mkdir(parents=True)

    # Tmuxinator config
    if template.tmuxinator:
        tmux_config = TMUXINATOR_TEMPLATE.format(name=ws_name, workspace_path=workspace_path)
        tmux_file = workspace_dir / ".tmuxinator.yml"
        tmux_file.write_text(tmux_config)

        # Symlink for tmuxinator to find it
        tmux_config_dir = Path.home() / ".config" / "tmuxinator"
        tmux_config_dir.mkdir(parents=True, exist_ok=True)
        link = tmux_config_dir / f"{ws_name}.yml"
        link.unlink(missing_ok=True)
        link.symlink_to(tmux_file)

    return workspace_path


def shell_local_workspace(
    ws_name: str,
    workspace_path: str,
    *,
    use_tmuxinator: bool = True,
    tmuxinator_enabled: bool = True,
) -> None:
    """Open a shell into a local workspace."""
    if use_tmuxinator and tmuxinator_enabled:
        os.execlp("tmuxinator", "tmuxinator", "start", ws_name)
    else:
        shell = os.environ.get("SHELL", "/bin/sh")
        os.chdir(workspace_path)
        os.execlp(shell, shell, "-l")


def delete_local_workspace(ws_name: str, workspace_path: str) -> None:
    """Delete a local workspace directory."""
    ws_dir = Path(workspace_path)
    if ws_dir.exists():
        shutil.rmtree(ws_dir)

    # Remove tmuxinator symlink
    link = Path.home() / ".config" / "tmuxinator" / f"{ws_name}.yml"
    link.unlink(missing_ok=True)
