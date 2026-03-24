"""Local workspace backend -- operations directly on the host filesystem."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from agentworks.workspaces.tmuxinator import generate_config

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.workspaces.templates import ResolvedTemplate


def create_local_workspace(
    config: Config,
    ws_name: str,
    template: ResolvedTemplate,
) -> str:
    """Create a local workspace. Returns the workspace path.

    Idempotent: if the workspace directory already exists (e.g. from a
    previous interrupted attempt), it is removed and recreated.
    """
    workspace_dir = config.paths.local_workspaces / ws_name
    workspace_path = str(workspace_dir)

    if workspace_dir.exists():
        typer.echo("  Removing stale workspace directory from previous attempt...")
        shutil.rmtree(workspace_dir)

    # Git clone or just create directory
    if template.repo:
        typer.echo(f"Cloning {template.repo}...")
        try:
            result = subprocess.run(
                ["git", "clone", template.repo, workspace_path],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            typer.echo("Error: git clone timed out after 5 minutes", err=True)
            raise typer.Exit(1) from None
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

    # Tmuxinator config (no agents on local workspaces)
    if template.tmuxinator:
        tmux_config = generate_config(ws_name, workspace_path)
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
