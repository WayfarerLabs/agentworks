"""Typer CLI entrypoint for Agentworks."""

from __future__ import annotations

import secrets
from typing import Annotated

import click
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

agent_app = typer.Typer(
    name="agent",
    help="Manage agents (isolated users within workspaces).",
    no_args_is_help=True,
)
app.add_typer(agent_app)

installer_app = typer.Typer(
    name="installer",
    help="List and inspect available installers from the catalog.",
    no_args_is_help=True,
)
app.add_typer(installer_app)


# -- Helpers ---------------------------------------------------------------


def _get_db() -> Database:
    return Database()


def _generate_name() -> str:
    return secrets.token_hex(4)


def _prompt_name(label: str, name: str | None) -> str:
    """Prompt for a name if not provided via --name, showing a random default."""
    if name is not None:
        return name
    default = _generate_name()
    return str(typer.prompt(f"{label} name", default=default))


# -- Top-level commands ----------------------------------------------------


@app.command("completion")
def completion(
    shell: Annotated[str, typer.Argument(help="Shell type", click_type=click.Choice(["bash", "zsh", "powershell"]))] = "zsh",
    install: Annotated[bool, typer.Option("--install", help="Install completions to the default location")] = False,
) -> None:
    """Output shell completion script (or install it with --install)."""
    from agentworks.completions import SUPPORTED_SHELLS, generate
    from agentworks.completions.install import install_completions

    if shell not in SUPPORTED_SHELLS:
        typer.echo(f"Error: unsupported shell '{shell}'. Supported: {', '.join(SUPPORTED_SHELLS)}", err=True)
        raise typer.Exit(1)

    script = generate(shell)

    if install:
        install_completions(shell, script)
    else:
        typer.echo(script, nl=False)


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
    platform: Annotated[
        str | None, typer.Option("--platform", help="Platform", click_type=click.Choice(["lima", "azure", "wsl2"]))
    ] = None,
    vm_host: Annotated[str | None, typer.Option("--vm-host", help="VM host for Lima")] = None,
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
        cpus=cpus, memory=memory, disk=disk,
        azure_vm_size=azure_vm_size, vm_user=vm_user,
    )


@vm_app.command("list")
def vm_list() -> None:
    """List VMs."""
    from agentworks.vms.manager import list_vms

    list_vms(_get_db())


@vm_app.command("describe")
def vm_describe(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Show detailed information about a VM."""
    from agentworks.vms.manager import describe_vm

    describe_vm(_get_db(), name)


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
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a VM and clean up all resources."""
    from agentworks.config import load_config
    from agentworks.vms.manager import delete_vm

    delete_vm(_get_db(), load_config(), name, force=force, yes=yes)


@vm_app.command("reinit")
def vm_reinit(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Re-run initialization on a provisioned VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import reinit_vm

    reinit_vm(_get_db(), load_config(), name)


@vm_app.command("shell")
def vm_shell(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Open a shell on a VM (home directory)."""
    from agentworks.config import load_config
    from agentworks.vms.manager import shell_vm

    shell_vm(_get_db(), load_config(), name)


@vm_app.command("add-git-credential")
def vm_add_git_credential(
    name: Annotated[str, typer.Argument(help="VM name")],
    credential: Annotated[str, typer.Argument(help="Git credential name from config")],
) -> None:
    """Add or update a git credential on a VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import add_git_credential

    add_git_credential(_get_db(), load_config(), name, credential)


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
    local: Annotated[bool, typer.Option("--local", help="Show only local workspaces")] = False,
) -> None:
    """List workspaces."""
    from agentworks.workspaces.manager import list_workspaces

    if local and vm:
        typer.echo("Error: --local and --vm are mutually exclusive", err=True)
        raise typer.Exit(1)

    ws_type = "local" if local else None
    list_workspaces(_get_db(), vm_name=vm, ws_type=ws_type)


@workspace_app.command("delete")
def workspace_delete(
    name: Annotated[str, typer.Argument(help="Workspace name")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a workspace."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import delete_workspace

    delete_workspace(_get_db(), load_config(), name, yes=yes)


# -- Agent commands --------------------------------------------------------


@agent_app.command("create")
def agent_create(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspace: Annotated[str, typer.Option("--workspace", help="Workspace name")] = ...,  # type: ignore[assignment]
) -> None:
    """Create an agent (isolated Linux user) on a workspace."""
    from agentworks.agents.manager import create_agent
    from agentworks.config import load_config

    create_agent(_get_db(), load_config(), name=name, workspace_name=workspace)


@agent_app.command("list")
def agent_list(
    workspace: Annotated[str | None, typer.Option("--workspace", help="Filter by workspace")] = None,
) -> None:
    """List agents."""
    from agentworks.agents.manager import list_agents

    list_agents(_get_db(), workspace_name=workspace)


@agent_app.command("shell")
def agent_shell(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspace: Annotated[str, typer.Option("--workspace", help="Workspace name")] = ...,  # type: ignore[assignment]
) -> None:
    """Open a shell as an agent user."""
    from agentworks.agents.manager import shell_agent
    from agentworks.config import load_config

    shell_agent(_get_db(), load_config(), name=name, workspace_name=workspace)


@agent_app.command("delete")
def agent_delete(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspace: Annotated[str, typer.Option("--workspace", help="Workspace name")] = ...,  # type: ignore[assignment]
) -> None:
    """Delete an agent from a workspace."""
    from agentworks.agents.manager import delete_agent
    from agentworks.config import load_config

    delete_agent(_get_db(), load_config(), name=name, workspace_name=workspace)


# -- Installer catalog commands --------------------------------------------

_TYPE_CHOICES = click.Choice(["apt-source", "apt-package", "system-install-cmd", "user-install-cmd"])


@installer_app.command("list")
def installer_list(
    type_filter: Annotated[
        str | None, typer.Option("--type", help="Filter by type", click_type=_TYPE_CHOICES)
    ] = None,
    source_filter: Annotated[
        str | None, typer.Option("--source", help="Filter by source", click_type=click.Choice(["builtin", "user"]))
    ] = None,
) -> None:
    """List available installers from the built-in and user catalog."""
    from agentworks.catalog import load_builtin_catalog, load_catalog
    from agentworks.config import load_config

    config = load_config()
    builtin = load_builtin_catalog()
    merged = load_catalog(config)

    rows: list[tuple[str, str, str, str]] = []  # (type, name, source, description)

    def _add_entries(
        type_label: str,
        merged_entries: dict[str, object],
        builtin_entries: dict[str, object],
    ) -> None:
        for name, entry in sorted(merged_entries.items()):
            is_builtin = name in builtin_entries
            is_user = name in getattr(config, _CONFIG_ATTR[type_label], {})
            if is_user:
                source = "user"
            elif is_builtin:
                source = "built-in"
            else:
                source = "built-in"
            if source_filter == "builtin" and source != "built-in":
                continue
            if source_filter == "user" and source != "user":
                continue
            rows.append((type_label, name, source, entry.description))

    if type_filter is None or type_filter == "apt-source":
        _add_entries("apt-source", merged.apt_sources, builtin.apt_sources)
    if type_filter is None or type_filter == "apt-package":
        _add_entries("apt-package", merged.apt_packages, builtin.apt_packages)
    if type_filter is None or type_filter == "system-install-cmd":
        _add_entries("system-install-cmd", merged.system_install_commands, builtin.system_install_commands)
    if type_filter is None or type_filter == "user-install-cmd":
        _add_entries("user-install-cmd", merged.user_install_commands, builtin.user_install_commands)

    if not rows:
        typer.echo("No entries found.")
        return

    # Calculate column widths
    type_w = max(len(r[0]) for r in rows)
    name_w = max(len(r[1]) for r in rows)
    src_w = max(len(r[2]) for r in rows)

    header = f"{'TYPE':<{type_w}}  {'NAME':<{name_w}}  {'SOURCE':<{src_w}}  DESCRIPTION"
    typer.echo(header)
    typer.echo("-" * len(header))
    for type_label, name, source, desc in rows:
        typer.echo(f"{type_label:<{type_w}}  {name:<{name_w}}  {source:<{src_w}}  {desc}")


# Maps type labels to config attributes for source detection
_CONFIG_ATTR = {
    "apt-source": "apt_sources",
    "apt-package": "apt_packages",
    "system-install-cmd": "system_install_commands",
    "user-install-cmd": "user_install_commands",
}


@installer_app.command("describe")
def installer_describe(
    name: Annotated[str, typer.Argument(help="Entry name")],
) -> None:
    """Show details of a catalog entry."""
    from agentworks.catalog import (
        AptPackageEntry,
        AptSourceEntry,
        SystemInstallCommandEntry,
        UserInstallCommandEntry,
        load_builtin_catalog,
        load_catalog,
    )
    from agentworks.config import load_config

    config = load_config()
    builtin = load_builtin_catalog()
    merged = load_catalog(config)

    # Search all four pools
    for type_label, merged_entries, builtin_entries, config_attr in [
        ("apt-source", merged.apt_sources, builtin.apt_sources, "apt_sources"),
        ("apt-package", merged.apt_packages, builtin.apt_packages, "apt_packages"),
        ("system-install-cmd", merged.system_install_commands, builtin.system_install_commands, "system_install_commands"),
        ("user-install-cmd", merged.user_install_commands, builtin.user_install_commands, "user_install_commands"),
    ]:
        if name not in merged_entries:
            continue

        entry = merged_entries[name]
        is_user = name in getattr(config, config_attr, {})
        source = "user" if is_user else "built-in"
        overrides = name in builtin_entries and is_user

        typer.echo(f"Name:        {name}")
        typer.echo(f"Type:        {type_label}")
        typer.echo(f"Source:      {source}")
        if overrides:
            typer.echo("             (overrides built-in)")
        typer.echo(f"Description: {entry.description}")

        if isinstance(entry, AptSourceEntry):
            typer.echo(f"Key URL:     {entry.key_url}")
            typer.echo(f"Key path:    {entry.key_path}")
            typer.echo(f"Source:      {entry.source}")
            typer.echo(f"Source file: {entry.source_file}")
            if entry.key_dearmor:
                typer.echo("Key dearmor: yes")
        elif isinstance(entry, AptPackageEntry):
            if entry.apt_sources:
                typer.echo(f"Apt sources: {', '.join(entry.apt_sources)}")
            typer.echo(f"Apt:         {', '.join(entry.apt)}")
        elif isinstance(entry, (SystemInstallCommandEntry, UserInstallCommandEntry)):
            typer.echo(f"Command:     {entry.command}")
            if entry.path:
                typer.echo(f"PATH:        {', '.join(entry.path)}")
        return

    typer.echo(f"Error: '{name}' not found in catalog", err=True)
    raise typer.Exit(1)
