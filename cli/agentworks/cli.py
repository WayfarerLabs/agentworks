"""Typer CLI entrypoint for Agentworks."""

from __future__ import annotations

import secrets
from typing import Annotated

import typer

from agentworks.db import Database

app = typer.Typer(
    name="agentworks",
    help="Orchestrate workspace lifecycle across multiple compute targets.",
    no_args_is_help=True,
)

# -- Command groups --------------------------------------------------------

vm_host_app = typer.Typer(
    name="vm-host",
    help="Manage VM hosts (machines that run VMs).",
    no_args_is_help=True,
)
app.add_typer(vm_host_app)

vm_app = typer.Typer(
    name="vm",
    help="Manage virtual machines.",
    no_args_is_help=True,
)
app.add_typer(vm_app)

workspace_app = typer.Typer(
    name="workspace",
    help="Manage workspaces.",
    no_args_is_help=True,
)
app.add_typer(workspace_app)


# -- Helpers ---------------------------------------------------------------


def _get_db() -> Database:
    return Database()


def _generate_name() -> str:
    return secrets.token_hex(4)[:7]


def _prompt_name(label: str, name: str | None) -> str:
    """Prompt for a name if not provided via --name, showing a random default."""
    if name is not None:
        return name
    default = _generate_name()
    return str(typer.prompt(f"{label} name", default=default))


# -- Top-level commands ----------------------------------------------------


@app.command("completion")
def completion(
    shell: Annotated[str, typer.Argument(help="Shell type: zsh, powershell")] = "zsh",
) -> None:
    """Output shell completion script."""
    from agentworks.completions import SUPPORTED_SHELLS, generate

    if shell not in SUPPORTED_SHELLS:
        typer.echo(f"Error: unsupported shell '{shell}'. Supported: {', '.join(SUPPORTED_SHELLS)}", err=True)
        raise typer.Exit(1)

    typer.echo(generate(shell), nl=False)


@app.command("doctor")
def doctor() -> None:
    """Check environment, config, and dependencies."""
    from agentworks.doctor import run_doctor

    run_doctor()


@app.command("init")
def init_config() -> None:
    """Create a sample config file at ~/.config/agentworks/config.toml."""
    import shutil
    from importlib.resources import files

    from agentworks.config import CONFIG_DIR, CONFIG_PATH

    if CONFIG_PATH.exists():
        typer.echo(f"Config already exists: {CONFIG_PATH}")
        typer.echo("Edit it directly, or remove it and run 'agentworks init' again.")
        return

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    sample = files("agentworks").joinpath("sample-config.toml")
    shutil.copy2(str(sample), CONFIG_PATH)
    typer.echo(f"Sample config written to {CONFIG_PATH}")
    typer.echo("Edit it to match your setup, then run 'agentworks vm create' to get started.")


# -- VM Host commands ------------------------------------------------------


@vm_host_app.command("add")
def vm_host_add(
    name: Annotated[str, typer.Argument(help="Name for this VM host")],
    ssh_host: Annotated[str, typer.Argument(help="SSH address (hostname or IP)")],
) -> None:
    """Register a new VM host."""
    from agentworks.vm_hosts.manager import add_vm_host

    add_vm_host(_get_db(), name, ssh_host)


@vm_host_app.command("list")
def vm_host_list() -> None:
    """List registered VM hosts."""
    from agentworks.vm_hosts.manager import list_vm_hosts

    list_vm_hosts(_get_db())


@vm_host_app.command("remove")
def vm_host_remove(
    name: Annotated[str, typer.Argument(help="Name of the VM host to remove")],
    force: Annotated[bool, typer.Option("--force", help="Remove even if VMs reference this host")] = False,
) -> None:
    """Remove a VM host."""
    from agentworks.vm_hosts.manager import remove_vm_host

    remove_vm_host(_get_db(), name, force=force)


# -- VM commands -----------------------------------------------------------


@vm_app.command("create")
def vm_create(
    name: Annotated[str | None, typer.Option("--name", help="VM name (prompted if omitted)")] = None,
    platform: Annotated[str | None, typer.Option("--platform", help="Platform: lima, azure, wsl2")] = None,
    vm_host: Annotated[str | None, typer.Option("--vm-host", help="VM host for Lima")] = None,
    extra_packages: Annotated[
        list[str] | None, typer.Option("--extra-packages", help="Additional apt packages")
    ] = None,
    git_hosts: Annotated[list[str] | None, typer.Option("--git-hosts", help="Git hosts to register")] = None,
    cpus: Annotated[int | None, typer.Option("--cpus", help="Number of CPUs")] = None,
    memory: Annotated[int | None, typer.Option("--memory", help="Memory in GiB")] = None,
    disk: Annotated[int | None, typer.Option("--disk", help="Disk size in GiB")] = None,
    azure_vm_size: Annotated[
        str | None, typer.Option("--azure-vm-size", help="Azure VM size")
    ] = None,
    vm_user: Annotated[str | None, typer.Option("--vm-user", help="Username on the VM")] = None,
) -> None:
    """Create a new VM (provision + initialize)."""
    from agentworks.config import load_config
    from agentworks.vms.manager import create_vm

    resolved_name = _prompt_name("VM", name)
    config = load_config()
    create_vm(
        _get_db(), config,
        name=resolved_name, platform=platform, vm_host=vm_host,
        extra_packages=extra_packages, git_hosts=git_hosts,
        cpus=cpus, memory=memory, disk=disk,
        azure_vm_size=azure_vm_size, vm_user=vm_user,
    )


@vm_app.command("list")
def vm_list() -> None:
    """List VMs."""
    from agentworks.vms.manager import list_vms

    list_vms(_get_db())


@vm_app.command("start")
def vm_start(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Start a stopped VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import start_vm

    start_vm(_get_db(), load_config(), name)


@vm_app.command("stop")
def vm_stop(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Stop a running VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import stop_vm

    stop_vm(_get_db(), load_config(), name)


@vm_app.command("delete")
def vm_delete(
    name: Annotated[str, typer.Argument(help="VM name")],
    force: Annotated[bool, typer.Option("--force", help="Force delete even with workspaces")] = False,
) -> None:
    """Delete a VM and clean up all resources."""
    from agentworks.config import load_config
    from agentworks.vms.manager import delete_vm

    delete_vm(_get_db(), load_config(), name, force=force)


@vm_app.command("shell")
def vm_shell(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Open a shell on a VM (home directory)."""
    from agentworks.config import load_config
    from agentworks.vms.manager import shell_vm

    shell_vm(_get_db(), load_config(), name)


# -- Workspace commands ----------------------------------------------------


@workspace_app.command("create")
def workspace_create(
    name: Annotated[str | None, typer.Option("--name", help="Workspace name (prompted if omitted)")] = None,
    vm: Annotated[str | None, typer.Option("--vm", help="Target VM")] = None,
    local: Annotated[bool, typer.Option("--local", help="Create a local workspace (no VM)")] = False,
    template: Annotated[str | None, typer.Option("--template", help="Workspace template")] = None,
    open_vscode: Annotated[bool, typer.Option("--open-vscode", help="Open in VS Code")] = False,
) -> None:
    """Create a workspace on a VM or locally."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import create_workspace

    if local and vm:
        typer.echo("Error: --local and --vm are mutually exclusive", err=True)
        raise typer.Exit(1)

    resolved_name = _prompt_name("Workspace", name)
    create_workspace(
        _get_db(), load_config(),
        name=resolved_name, vm_name=vm, local=local,
        template_name=template, open_vscode=open_vscode,
    )


@workspace_app.command("shell")
def workspace_shell(
    name: Annotated[str, typer.Argument(help="Workspace name")],
    no_tmuxinator: Annotated[bool, typer.Option("--no-tmuxinator", help="Skip tmuxinator")] = False,
) -> None:
    """Open a shell into a workspace."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import shell_workspace

    shell_workspace(_get_db(), load_config(), name, no_tmuxinator=no_tmuxinator)


@workspace_app.command("list")
def workspace_list(
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM")] = None,
) -> None:
    """List workspaces."""
    from agentworks.workspaces.manager import list_workspaces

    list_workspaces(_get_db(), vm_name=vm)


@workspace_app.command("delete")
def workspace_delete(
    name: Annotated[str, typer.Argument(help="Workspace name")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a workspace."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import delete_workspace

    delete_workspace(_get_db(), load_config(), name, yes=yes)
