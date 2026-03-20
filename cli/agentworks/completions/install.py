"""Install shell completions to the default location."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import typer


def install_completions(shell: str, script: str) -> None:
    """Write the completion script to the appropriate location."""
    if shell == "bash":
        _install_bash(script)
    elif shell == "zsh":
        _install_zsh(script)
    elif shell == "powershell":
        _install_powershell(script)
    else:
        typer.echo(f"Error: --install not supported for '{shell}'", err=True)
        raise typer.Exit(1)


def _install_bash(script: str) -> None:
    """Install bash completions to the standard user directory."""
    target_dir = Path.home() / ".local" / "share" / "bash-completion" / "completions"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "agentworks"
    target.write_text(script)
    typer.echo(f"Installed to {target}")

    # Check if bash-completion is likely available
    bashrc = Path.home() / ".bashrc"
    if bashrc.exists():
        content = bashrc.read_text()
        if "bash-completion" in content or "bash_completion" in content:
            return
    typer.echo("Note: ensure bash-completion is installed and loaded in your .bashrc")


def _install_zsh(script: str) -> None:
    """Install zsh completions to Oh My Zsh custom dir or ~/.zfunc."""
    home = Path.home()

    # Prefer Oh My Zsh if present
    zsh_custom = os.environ.get("ZSH_CUSTOM")
    if zsh_custom:
        target_dir = Path(zsh_custom) / "completions"
    elif (home / ".oh-my-zsh" / "custom").is_dir():
        target_dir = home / ".oh-my-zsh" / "custom" / "completions"
    else:
        target_dir = home / ".zfunc"

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "_agentworks"
    target.write_text(script)
    typer.echo(f"Installed to {target}")

    # Check if ~/.zfunc needs fpath setup (not needed for Oh My Zsh)
    if target_dir.name == ".zfunc":
        typer.echo("Note: ensure your .zshrc has: fpath=(~/.zfunc $fpath)")


def _install_powershell(script: str) -> None:
    """Install PowerShell completions and update $PROFILE to source them."""
    profile_path = _query_powershell_profile()
    if profile_path is None:
        typer.echo("Error: could not determine PowerShell $PROFILE path", err=True)
        typer.echo("Is powershell or pwsh installed and on PATH?", err=True)
        raise typer.Exit(1)

    # Install completions next to the profile
    target_dir = profile_path.parent / "Completions"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "agentworks.ps1"
    target.write_text(script)
    typer.echo(f"Installed to {target}")

    # Ensure $PROFILE sources the completion script
    if profile_path.exists():
        content = profile_path.read_text()
        if "agentworks.ps1" in content:
            typer.echo("$PROFILE already sources agentworks completions")
            return
    else:
        content = ""

    source_line = f'. "{target}"'
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    with profile_path.open("a") as f:
        if content and not content.endswith("\n"):
            f.write("\n")
        f.write(f"{source_line}\n")
    typer.echo(f"Added to $PROFILE: {profile_path}")


def _query_powershell_profile() -> Path | None:
    """Ask PowerShell for the actual $PROFILE path.

    Tries pwsh (PowerShell Core) first, then powershell (Windows PowerShell).
    Uses -NoProfile to avoid loading a broken profile during the query.
    """
    for cmd in ("pwsh", "powershell"):
        if not shutil.which(cmd):
            continue
        try:
            result = subprocess.run(
                [cmd, "-NoProfile", "-Command", "$PROFILE"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            path = result.stdout.strip()
            if result.returncode == 0 and path:
                return Path(path)
        except (subprocess.TimeoutExpired, OSError):
            continue
    return None
