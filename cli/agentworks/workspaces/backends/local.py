"""Local workspace backend -- operations directly on the host filesystem."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from agentworks import output
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

    Errors if the workspace directory already exists.
    """
    workspace_dir = config.paths.local_workspaces / ws_name
    workspace_path = str(workspace_dir)

    if workspace_dir.exists():
        raise output.WorkspaceError(
            f"directory {workspace_path} already exists.\nRemove it manually or choose a different name."
        )

    # Git clone or just create directory
    if template.repo:
        output.info(f"Cloning {template.repo}...")
        try:
            result = subprocess.run(
                ["git", "clone", template.repo, workspace_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            raise output.WorkspaceError("git clone timed out after 5 minutes") from None
        if result.returncode != 0:
            if template.repo.startswith("https://"):
                output.detail(
                    "Hint: HTTPS repo URLs require credentials. "
                    "For private repos, use an SSH URL (git@...) so "
                    "your SSH key provides authentication."
                )
            raise output.WorkspaceError(f"git clone failed: {result.stderr.strip()}")
    else:
        workspace_dir.mkdir(parents=True)

    # Tmuxinator config (no agents on local workspaces)
    if template.tmuxinator:
        tmux_config = generate_config(ws_name, workspace_path)
        tmux_file = workspace_dir / ".tmuxinator.yml"
        tmux_file.write_text(tmux_config)

        # Symlink for tmuxinator to find it by console session name
        from agentworks.workspaces.tmuxinator import console_session_name

        session = console_session_name(ws_name)
        tmux_config_dir = Path.home() / ".config" / "tmuxinator"
        tmux_config_dir.mkdir(parents=True, exist_ok=True)
        link = tmux_config_dir / f"{session}.yml"
        link.unlink(missing_ok=True)
        link.symlink_to(tmux_file)

    return workspace_path


def shell_local_workspace(
    workspace_path: str,
) -> None:
    """Open a plain shell into a local workspace."""
    shell = os.environ.get("SHELL", "/bin/sh")
    os.chdir(workspace_path)
    os.execlp(shell, shell, "-l")


def console_local_workspace(
    ws_name: str,
    *,
    recreate: bool = False,
) -> None:
    """Open the workspace console (tmuxinator) for a local workspace."""
    import subprocess

    from agentworks.workspaces.tmuxinator import console_session_name

    session = console_session_name(ws_name)

    if recreate:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)  # noqa: S603, S607

    os.execlp("tmuxinator", "tmuxinator", "start", session)


def delete_local_workspace(ws_name: str, workspace_path: str) -> None:
    """Delete a local workspace directory."""
    ws_dir = Path(workspace_path)
    if ws_dir.exists():
        shutil.rmtree(ws_dir)

    # Remove tmuxinator symlink
    from agentworks.workspaces.tmuxinator import console_session_name

    session = console_session_name(ws_name)
    link = Path.home() / ".config" / "tmuxinator" / f"{session}.yml"
    link.unlink(missing_ok=True)
