"""`agentworks config` -- configuration utilities."""

from __future__ import annotations

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db

config_app = typer.Typer(
    name="config",
    help="Configuration utilities.",
    no_args_is_help=True,
)
app.add_typer(config_app)


@config_app.command("init")
def config_init() -> None:
    """Create a sample config file at ~/.config/agentworks/config.toml."""
    import shutil
    from importlib.resources import files

    from agentworks.config import CONFIG_DIR, CONFIG_PATH

    if CONFIG_PATH.exists():
        typer.echo(f"Config already exists: {CONFIG_PATH}")
        typer.echo("Edit it directly, or remove it and run 'agentworks config init' again.")
        return

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    sample = files("agentworks").joinpath("sample-config.toml")
    shutil.copy2(str(sample), CONFIG_PATH)
    typer.echo(f"Sample config written to {CONFIG_PATH}")
    typer.echo("Edit it to match your setup, then run 'agentworks vm create' to get started.")


@config_app.command("edit")
def config_edit() -> None:
    """Open the config file in your editor ($EDITOR)."""
    import os
    import subprocess
    import sys

    from agentworks.config import CONFIG_PATH

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        typer.echo("Error: $EDITOR is not set. Set it to your preferred editor.", err=True)
        raise typer.Exit(1)

    if not CONFIG_PATH.exists():
        typer.echo(f"Error: config file not found at {CONFIG_PATH}", err=True)
        typer.echo("Run 'agentworks config init' to create one.", err=True)
        raise typer.Exit(1)

    sys.exit(subprocess.call([editor, str(CONFIG_PATH)]))


@config_app.command("sample")
def config_sample() -> None:
    """Print the sample config to stdout."""
    from importlib.resources import files

    sample = files("agentworks").joinpath("sample-config.toml")
    typer.echo(sample.read_text(), nl=False)


@config_app.command("sync-vscode-workspaces")
def config_sync_vscode_workspaces() -> None:
    """Regenerate .code-workspace files for all VM workspaces."""
    from agentworks.config import load_config
    from agentworks.workspaces.backends.vm import generate_vscode_workspace

    config = load_config()
    db = get_db()

    workspaces = db.list_workspaces()
    if not workspaces:
        typer.echo("No workspaces found.")
        return

    count = 0
    for ws in workspaces:
        vm = db.get_vm(ws.vm_name)
        if vm is None:
            typer.echo(f"  Skipping '{ws.name}': VM '{ws.vm_name}' not found", err=True)
            continue
        path = generate_vscode_workspace(vm, config, ws.name, ws.workspace_path)
        typer.echo(f"  {ws.name} -> {path}")
        count += 1

    typer.echo(f"Regenerated {count} VS Code workspace file(s) in {config.paths.vscode_workspaces}")


@config_app.command("sync-ssh-config")
def config_sync_ssh_config() -> None:
    """Rebuild SSH config entries for all VMs from current state."""
    from agentworks.config import load_config
    from agentworks.ssh_config import sync_ssh_config

    sync_ssh_config(load_config(), get_db())
