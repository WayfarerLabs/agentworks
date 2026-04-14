"""Typer CLI entrypoint for Agentworks."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Annotated, Protocol

import click
import typer

from agentworks.db import Database

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    help="Manage agents (isolated users on VMs).",
    no_args_is_help=True,
)
app.add_typer(agent_app)

agent_grants_app = typer.Typer(
    name="workspace-grants",
    help="Manage agent workspace access grants.",
    no_args_is_help=True,
)
agent_app.add_typer(agent_grants_app)

session_app = typer.Typer(
    name="session",
    help="Manage sessions.",
    no_args_is_help=True,
)
app.add_typer(session_app)

installer_app = typer.Typer(
    name="installer",
    help="List and inspect available installers from the catalog.",
    no_args_is_help=True,
)
app.add_typer(installer_app)

config_app = typer.Typer(
    name="config",
    help="Configuration utilities.",
    no_args_is_help=True,
)
app.add_typer(config_app)


# -- Global options --------------------------------------------------------

_non_interactive = False


@app.callback()
def _global_options(
    non_interactive: Annotated[
        bool,
        typer.Option("--non-interactive", help="Disable interactive prompts"),
    ] = False,
) -> None:
    """Global options for all commands."""
    global _non_interactive  # noqa: PLW0603
    _non_interactive = non_interactive


# -- Helpers ---------------------------------------------------------------


def _get_db() -> Database:
    return Database()


def _generate_name() -> str:
    return secrets.token_hex(4)


def _is_interactive() -> bool:
    """Check if stdin is a TTY and --non-interactive was not passed."""
    import sys

    if _non_interactive:
        return False

    return sys.stdin.isatty()


def _require_interactive(what: str) -> None:
    """Raise if not interactive and a prompt would be needed."""
    if not _is_interactive():
        typer.echo(f"Error: {what} is required in non-interactive mode", err=True)
        raise typer.Exit(1)


def _prompt_name(label: str, name: str | None) -> str:
    """Prompt for a name if not provided via --name, showing a random default."""
    if name is not None:
        return name
    _require_interactive("--name")
    default = _generate_name()
    return str(typer.prompt(f"{label} name", default=default))


def _prompt_workspace(db: Database, workspace: str | None) -> str:
    """Prompt for a workspace if not provided, listing available workspaces."""
    if workspace is not None:
        return workspace

    workspaces = db.list_workspaces()
    if not workspaces:
        typer.echo("Error: no workspaces found. Create one with 'agentworks workspace create'.", err=True)
        raise typer.Exit(1)

    if len(workspaces) == 1:
        typer.echo(f"Using workspace '{workspaces[0].name}'")
        return workspaces[0].name

    _require_interactive("--workspace")

    typer.echo("Select a workspace:")
    for i, ws in enumerate(workspaces, 1):
        label = f"  {i}) {ws.name}"
        if ws.vm_name:
            label += f"  (vm: {ws.vm_name})"
        elif ws.type == "local":
            label += "  (local)"
        typer.echo(label)

    choice = int(typer.prompt("Workspace number", type=int))
    if choice < 1 or choice > len(workspaces):
        typer.echo(f"Error: invalid choice {choice}", err=True)
        raise typer.Exit(1)

    return workspaces[choice - 1].name


def _prompt_vm(db: Database, vm_name: str | None) -> str:
    """Prompt for a VM if not provided, listing available VMs."""
    if vm_name is not None:
        return vm_name

    vms = db.list_vms()
    if not vms:
        typer.echo("Error: no VMs found. Create one with 'agentworks vm create'.", err=True)
        raise typer.Exit(1)

    if len(vms) == 1:
        typer.echo(f"Using VM '{vms[0].name}'")
        return vms[0].name

    _require_interactive("--vm")

    typer.echo("Select a VM:")
    for i, v in enumerate(vms, 1):
        typer.echo(f"  {i}) {v.name}  ({v.platform})")

    choice = int(typer.prompt("VM number", type=int))
    if choice < 1 or choice > len(vms):
        typer.echo(f"Error: invalid choice {choice}", err=True)
        raise typer.Exit(1)

    return vms[choice - 1].name


class _HasDescription(Protocol):
    """Structural protocol for catalog entries that have a description."""

    @property
    def description(self) -> str: ...


# -- Top-level commands ----------------------------------------------------


_SHELL_CHOICES = click.Choice(["bash", "zsh", "powershell"])


@app.command("completion")
def completion(
    shell: Annotated[str, typer.Argument(help="Shell type", click_type=_SHELL_CHOICES)] = "zsh",
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
    template: Annotated[str | None, typer.Option("--template", help="VM template")] = None,
    platform: Annotated[
        str | None, typer.Option("--platform", help="Platform", click_type=click.Choice(["lima", "azure", "wsl2", "proxmox"]))
    ] = None,
    vm_host: Annotated[str | None, typer.Option("--vm-host", help="VM host for Lima")] = None,
    cpus: Annotated[int | None, typer.Option("--cpus", help="Number of CPUs")] = None,
    memory: Annotated[int | None, typer.Option("--memory", help="Memory in GiB")] = None,
    disk: Annotated[int | None, typer.Option("--disk", help="Disk size in GiB")] = None,
    azure_vm_size: Annotated[str | None, typer.Option("--azure-vm-size", help="Azure VM size")] = None,
    admin_username: Annotated[str | None, typer.Option("--admin-username", help="Admin username on the VM")] = None,
) -> None:
    """Create a new VM (provision + initialize)."""
    from agentworks.config import load_config
    from agentworks.vms.manager import create_vm

    resolved_name = _prompt_name("VM", name)
    config = load_config()
    create_vm(
        _get_db(),
        config,
        name=resolved_name,
        template=template,
        platform=platform,
        vm_host=vm_host,
        cpus=cpus,
        memory=memory,
        disk=disk,
        azure_vm_size=azure_vm_size,
        admin_username=admin_username,
    )


@vm_app.command("list")
def vm_list() -> None:
    """List VMs."""
    from agentworks.vms.manager import list_vms

    list_vms(_get_db())


@vm_app.command("backup")
def vm_backup(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Create a full backup of a VM: metadata, agents, workspaces, and files."""
    from agentworks.config import load_config
    from agentworks.vms.backup import backup_vm

    backup_vm(_get_db(), load_config(), name)


@vm_app.command("describe")
def vm_describe(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Show detailed information about a VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import describe_vm

    describe_vm(_get_db(), load_config(), name)


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


@vm_app.command("port-forward")
def vm_port_forward(
    name: Annotated[str, typer.Argument(help="VM name")],
    ports: Annotated[list[str], typer.Argument(help="Port specs: [LOCAL_PORT:]REMOTE_PORT")],
    address: Annotated[str, typer.Option("--address", help="Local address to bind to")] = "localhost",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose SSH output")] = False,
) -> None:
    """Forward local port(s) to a VM (like kubectl port-forward)."""
    from agentworks.config import load_config
    from agentworks.vms.manager import port_forward_vm

    port_forward_vm(_get_db(), load_config(), name, ports, address=address, verbose=verbose)


@vm_app.command("add-git-credential")
def vm_add_git_credential(
    name: Annotated[str, typer.Argument(help="VM name")],
    credential: Annotated[str, typer.Argument(help="Git credential name from config")],
) -> None:
    """Add or update a git credential on a VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import add_git_credential

    add_git_credential(_get_db(), load_config(), name, credential)


@vm_app.command("logs")
def vm_logs(
    name: Annotated[str, typer.Argument(help="VM name")],
    show_all: Annotated[bool, typer.Option("--all", help="Show all logs instead of only the latest")] = False,
) -> None:
    """Show SSH logs for a VM."""
    from agentworks.ssh import LOG_DIR

    if not LOG_DIR.exists():
        typer.echo("No logs found.")
        return

    # Collect all logs for this VM -- filename is <vm>-<timestamp>-<cmd>.log
    all_logs = sorted(LOG_DIR.glob(f"{name}-*.log"), reverse=True)
    logs = [(str(p), p.name) for p in all_logs]

    if not logs:
        typer.echo(f"No SSH logs found for VM '{name}'.")
        return

    from pathlib import Path

    display = logs if show_all else logs[:1]
    for log_path, log_name in display:
        typer.echo(f"--- {log_name} ---")
        typer.echo(Path(log_path).read_text(), nl=False)
        typer.echo("")


@vm_app.command("console")
def vm_console(
    name: Annotated[str, typer.Argument(help="VM name")],
    recreate: Annotated[bool, typer.Option("--recreate", help="Kill and rebuild the console")] = False,
    allow_nesting: Annotated[bool, typer.Option("--allow-nesting", help="Allow running inside tmux")] = False,
) -> None:
    """Attach to the VM console (creates it if needed)."""
    from agentworks.config import load_config
    from agentworks.sessions.console import attach_console

    attach_console(
        _get_db(),
        load_config(),
        vm_name=name,
        recreate=recreate,
        allow_nesting=allow_nesting,
    )


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

    db = _get_db()

    # 1. Select target (VM), 2. Name
    if not local:
        vm = _prompt_vm(db, vm)
    resolved_name = _prompt_name("Workspace", name)

    create_workspace(
        db,
        load_config(),
        name=resolved_name,
        vm_name=vm,
        local=local,
        template_name=template,
        open_vscode=open_vscode,
    )


@workspace_app.command("shell")
def workspace_shell(
    name: Annotated[str, typer.Argument(help="Workspace name")],
) -> None:
    """Open a plain shell into a workspace."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import shell_workspace

    shell_workspace(_get_db(), load_config(), name)


@workspace_app.command("console")
def workspace_console(
    name: Annotated[str, typer.Argument(help="Workspace name")],
    recreate: Annotated[bool, typer.Option("--recreate", help="Kill and rebuild the console")] = False,
    allow_nesting: Annotated[bool, typer.Option("--allow-nesting", help="Allow running inside tmux")] = False,
) -> None:
    """Open the workspace console (tmux session with sessions)."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import console_workspace

    console_workspace(
        _get_db(),
        load_config(),
        name,
        allow_nesting=allow_nesting,
        recreate=recreate,
    )


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


@workspace_app.command("describe")
def workspace_describe(
    name: Annotated[str, typer.Argument(help="Workspace name")],
) -> None:
    """Show workspace details, sessions, and agent access."""
    from agentworks.workspaces.manager import describe_workspace

    describe_workspace(_get_db(), name)


@workspace_app.command("rehome")
def workspace_rehome(
    name: Annotated[str, typer.Argument(help="Workspace name")],
    target: Annotated[
        str | None, typer.Option("--target", help="Target path (default: configured workspace dir)")
    ] = None,
    remove_old: Annotated[
        bool, typer.Option("--remove-old", help="Remove the old directory after verified copy")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Move a workspace to a new directory path."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import rehome_workspace

    rehome_workspace(_get_db(), load_config(), name, target_path=target, remove_old=remove_old, yes=yes)


@workspace_app.command("repair")
def workspace_repair(
    name: Annotated[str, typer.Argument(help="Workspace name")],
) -> None:
    """Repair workspace infrastructure: group, permissions, ACLs, agent access."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import repair_workspace

    repair_workspace(_get_db(), load_config(), name)


@workspace_app.command("delete")
def workspace_delete(
    name: Annotated[str, typer.Argument(help="Workspace name")],
    force: Annotated[bool, typer.Option("--force", help="Force delete even with sessions")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a workspace."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import delete_workspace

    delete_workspace(_get_db(), load_config(), name, force=force, yes=yes)


@workspace_app.command("copy")
def workspace_copy(
    source: Annotated[str, typer.Argument(help="Source workspace name")],
    name: Annotated[str | None, typer.Option("--name", help="New workspace name (prompted if omitted)")] = None,
    vm: Annotated[str | None, typer.Option("--vm", help="Target VM")] = None,
    local: Annotated[bool, typer.Option("--local", help="Copy to a local workspace")] = False,
) -> None:
    """Copy a workspace to a new location (VM or local)."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import copy_workspace

    if local and vm:
        typer.echo("Error: --local and --vm are mutually exclusive", err=True)
        raise typer.Exit(1)

    resolved_name = _prompt_name("Workspace", name)
    copy_workspace(
        _get_db(),
        load_config(),
        source,
        dest_name=resolved_name,
        vm_name=vm,
        local=local,
    )


# -- Agent commands --------------------------------------------------------


@agent_app.command("create")
def agent_create(
    name: Annotated[str | None, typer.Option("--name", help="Agent name (prompted if omitted)")] = None,
    vm: Annotated[str | None, typer.Option("--vm", help="Target VM")] = None,
    template: Annotated[str | None, typer.Option("--template", help="Agent template")] = None,
    grant_all_workspaces: Annotated[
        bool,
        typer.Option("--grant-all-workspaces", help="Grant access to all workspaces"),
    ] = False,
) -> None:
    """Create an agent (isolated Linux user) on a VM."""
    from agentworks.agents.manager import create_agent
    from agentworks.config import load_config

    db = _get_db()

    # 1. Select target (VM), 2. Name
    resolved_vm = _prompt_vm(db, vm)
    resolved_name = _prompt_name("Agent", name)

    create_agent(
        db,
        load_config(),
        name=resolved_name,
        vm_name=resolved_vm,
        template=template,
        grant_all_workspaces=grant_all_workspaces,
    )


@agent_app.command("list")
def agent_list(
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM")] = None,
) -> None:
    """List agents."""
    from agentworks.agents.manager import list_agents

    list_agents(_get_db(), vm_name=vm)


@agent_app.command("describe")
def agent_describe(
    name: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    """Show detailed information about an agent."""
    from agentworks.agents.manager import describe_agent

    describe_agent(_get_db(), name=name)


@agent_app.command("reinit")
def agent_reinit(
    name: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    """Re-run agent setup using the stored template."""
    from agentworks.agents.manager import reinit_agent
    from agentworks.config import load_config

    reinit_agent(_get_db(), load_config(), name=name)


@agent_grants_app.command("grant")
def agent_grants_grant(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspaces: Annotated[str | None, typer.Argument(help="Workspace names (comma-separated)")] = None,
    all_workspaces: Annotated[bool, typer.Option("--all", help="Grant access to all workspaces")] = False,
) -> None:
    """Grant an agent explicit access to workspaces."""
    from agentworks.agents.manager import grant_workspaces
    from agentworks.config import load_config

    if not all_workspaces and not workspaces:
        typer.echo("Error: specify workspace names or --all", err=True)
        raise typer.Exit(1)

    ws_list = [w.strip() for w in workspaces.split(",")] if workspaces else []
    grant_workspaces(_get_db(), load_config(), agent_name=name, workspace_names=ws_list, grant_all=all_workspaces)


@agent_grants_app.command("deny")
def agent_grants_deny(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspaces: Annotated[str | None, typer.Argument(help="Workspace names (comma-separated)")] = None,
    all_workspaces: Annotated[bool, typer.Option("--all", help="Remove all explicit grants")] = False,
) -> None:
    """Remove explicit workspace grants from an agent."""
    from agentworks.agents.manager import deny_workspaces
    from agentworks.config import load_config

    if not all_workspaces and not workspaces:
        typer.echo("Error: specify workspace names or --all", err=True)
        raise typer.Exit(1)

    ws_list = [w.strip() for w in workspaces.split(",")] if workspaces else []
    deny_workspaces(_get_db(), load_config(), agent_name=name, workspace_names=ws_list, deny_all=all_workspaces)


@agent_grants_app.command("list")
def agent_grants_list(
    name: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    """List workspace grants for an agent."""
    from agentworks.agents.manager import list_grants

    list_grants(_get_db(), agent_name=name)


@agent_app.command("shell")
def agent_shell(
    name: Annotated[str, typer.Argument(help="Agent name")],
    workspace: Annotated[str | None, typer.Option("--workspace", help="cd into a workspace")] = None,
) -> None:
    """Open a shell as an agent user."""
    from agentworks.agents.manager import shell_agent
    from agentworks.config import load_config

    shell_agent(_get_db(), load_config(), name=name, workspace_name=workspace)


@agent_app.command("delete")
def agent_delete(
    name: Annotated[str, typer.Argument(help="Agent name")],
    force: Annotated[bool, typer.Option("--force", help="Force delete even with sessions")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete an agent."""
    from agentworks.agents.manager import delete_agent
    from agentworks.config import load_config

    delete_agent(_get_db(), load_config(), name=name, force=force, yes=yes)


# -- Session commands ---------------------------------------------------------


@session_app.command("create")
def session_create(
    name: Annotated[str | None, typer.Option("--name", help="Session name (prompted if omitted)")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", help="Existing workspace")] = None,
    template: Annotated[str | None, typer.Option("--template", help="Session template")] = None,
    admin: Annotated[bool, typer.Option("--admin", help="Run as the VM admin user")] = False,
    agent: Annotated[str | None, typer.Option("--agent", help="Agent name (agent mode)")] = None,
    new_workspace: Annotated[bool, typer.Option("--new-workspace", help="Create a new workspace")] = False,
    workspace_name: Annotated[str | None, typer.Option("--workspace-name", help="Name for new workspace")] = None,
    workspace_template: Annotated[
        str | None, typer.Option("--workspace-template", help="Template for new workspace")
    ] = None,
    vm: Annotated[str | None, typer.Option("--vm", help="VM for new workspace")] = None,
) -> None:
    """Create and start a session in a workspace."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import create_session
    from agentworks.workspaces.manager import create_workspace

    # Validate flag combinations before any prompts
    if admin and agent:
        typer.echo("Error: --admin and --agent are mutually exclusive", err=True)
        raise typer.Exit(1)
    if workspace and new_workspace:
        typer.echo("Error: --workspace and --new-workspace are mutually exclusive", err=True)
        raise typer.Exit(1)
    if not new_workspace and (workspace_name or workspace_template or vm):
        typer.echo(
            "Error: --workspace-name, --workspace-template, and --vm require --new-workspace",
            err=True,
        )
        raise typer.Exit(1)

    db = _get_db()
    config = load_config()

    if new_workspace:
        resolved_vm = _prompt_vm(db, vm)
        resolved_workspace = workspace_name  # may be None, resolved after session name

        # Resolve mode (need VM name for agent lookup)
        resolved_agent: str | None = agent
        if not admin and agent is None:
            # Look up agents on the target VM
            vm_agents = db.list_agents(vm_name=resolved_vm)
            if vm_agents:
                _require_interactive("--admin or --agent")
                typer.echo("Run session as:")
                typer.echo("  1) admin")
                for i, a in enumerate(vm_agents, 2):
                    label = f"agent: {a.name}"
                    if a.template:
                        label += f" [{a.template}]"
                    typer.echo(f"  {i}) {label}")
                choice = int(typer.prompt("Choice", type=int))
                if choice == 1:
                    resolved_agent = None
                else:
                    idx = choice - 2
                    if idx < 0 or idx >= len(vm_agents):
                        typer.echo(f"Error: invalid choice {choice}", err=True)
                        raise typer.Exit(1)
                    resolved_agent = vm_agents[idx].name

        resolved_name = _prompt_name("Session", name)
        resolved_ws_name = resolved_workspace or f"ws-{resolved_name}"

        create_workspace(
            db,
            config,
            name=resolved_ws_name,
            vm_name=resolved_vm,
            template_name=workspace_template,
        )
        resolved_workspace = resolved_ws_name
    else:
        resolved_workspace = _prompt_workspace(db, workspace)

        # Resolve mode
        resolved_agent: str | None = agent  # type: ignore[no-redef]
        if not admin and agent is None:
            resolved_agent = _prompt_session_mode(db, resolved_workspace)

        resolved_name = _prompt_name("Session", name)

    create_session(
        db,
        config,
        name=resolved_name,
        workspace_name=resolved_workspace,
        template_name=template,
        agent_name=resolved_agent,
        created_workspace=new_workspace,
    )


def _prompt_session_mode(db: Database, workspace_name: str) -> str | None:
    """Prompt for admin vs agent mode. Returns agent name or None for admin."""
    ws = db.get_workspace(workspace_name)
    if ws is None or ws.vm_name is None:
        return None

    agents = db.list_agents(vm_name=ws.vm_name)
    if not agents:
        # No agents on this VM, default to admin
        return None

    _require_interactive("--admin or --agent")

    typer.echo("Run session as:")
    typer.echo("  1) admin")
    for i, a in enumerate(agents, 2):
        label = f"agent: {a.name}"
        if a.template:
            label += f" [{a.template}]"
        typer.echo(f"  {i}) {label}")

    choice = int(typer.prompt("Choice", type=int))
    if choice == 1:
        return None
    idx = choice - 2
    if idx < 0 or idx >= len(agents):
        typer.echo(f"Error: invalid choice {choice}", err=True)
        raise typer.Exit(1)
    return agents[idx].name


@session_app.command("describe")
def session_describe(
    name: Annotated[str, typer.Argument(help="Session name")],
) -> None:
    """Show session details."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import describe_session

    describe_session(_get_db(), load_config(), name=name)


@session_app.command("list")
def session_list(
    workspace: Annotated[str | None, typer.Option("--workspace", help="Filter by workspace")] = None,
    no_status: Annotated[bool, typer.Option("--no-status", help="Skip SSH status check (faster)")] = False,
) -> None:
    """List sessions."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import list_sessions

    list_sessions(_get_db(), load_config(), workspace_name=workspace, no_status=no_status)


@session_app.command("stop")
def session_stop(
    name: Annotated[str, typer.Argument(help="Session name")],
) -> None:
    """Stop a running session."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import stop_session

    stop_session(_get_db(), load_config(), name=name)


@session_app.command("restart")
def session_restart(
    name: Annotated[str, typer.Argument(help="Session name")],
    force: Annotated[bool, typer.Option("--force", help="Kill if still running")] = False,
) -> None:
    """Restart a session (uses restart_command if defined in template)."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import restart_session

    restart_session(_get_db(), load_config(), name=name, force=force)


@session_app.command("attach")
def session_attach(
    name: Annotated[str, typer.Argument(help="Session name")],
) -> None:
    """Attach to a session."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import attach_session

    attach_session(_get_db(), load_config(), name=name)


@session_app.command("delete")
def session_delete(
    name: Annotated[str, typer.Argument(help="Session name")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Stop and delete a session."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import delete_session

    delete_session(_get_db(), load_config(), name=name, yes=yes)


@session_app.command("logs")
def session_logs(
    name: Annotated[str, typer.Argument(help="Session name")],
    lines: Annotated[int | None, typer.Option("--lines", "-n", help="Number of lines")] = None,
) -> None:
    """Dump the scrollback buffer for a session."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import session_logs as _session_logs

    _session_logs(_get_db(), load_config(), name=name, lines=lines)


# -- Installer catalog commands --------------------------------------------

_TYPE_CHOICES = click.Choice(["apt-source", "apt-package", "system-install-cmd", "user-install-cmd"])


@installer_app.command("list")
def installer_list(
    type_filter: Annotated[str | None, typer.Option("--type", help="Filter by type", click_type=_TYPE_CHOICES)] = None,
    source_filter: Annotated[
        str | None, typer.Option("--source", help="Filter by source", click_type=click.Choice(["builtin", "custom"]))
    ] = None,
) -> None:
    """List available installers from the built-in and custom catalog."""
    from agentworks.catalog import load_builtin_catalog, load_catalog
    from agentworks.config import load_config

    config = load_config()
    builtin = load_builtin_catalog()
    merged = load_catalog(config)

    rows: list[tuple[str, str, str, str]] = []  # (type, name, source, description)

    def _add_entries(
        type_label: str,
        merged_entries: Mapping[str, _HasDescription],
        builtin_entries: Mapping[str, _HasDescription],
    ) -> None:
        for name, entry in sorted(merged_entries.items()):
            is_builtin = name in builtin_entries
            is_custom = name in getattr(config, _CONFIG_ATTR[type_label], {})
            if is_custom:
                source = "custom"
            elif is_builtin:
                source = "built-in"
            else:
                source = "built-in"
            if source_filter == "builtin" and source != "built-in":
                continue
            if source_filter == "custom" and source != "custom":
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

    # Search all four pools; Mapping[str, _HasDescription] covers all catalog entry types
    # (all have description: str) and allows covariant use of the concrete dict types.
    pools: list[tuple[str, Mapping[str, _HasDescription], Mapping[str, _HasDescription], str]] = [
        ("apt-source", merged.apt_sources, builtin.apt_sources, "apt_sources"),
        ("apt-package", merged.apt_packages, builtin.apt_packages, "apt_packages"),
        (
            "system-install-cmd",
            merged.system_install_commands,
            builtin.system_install_commands,
            "system_install_commands",
        ),
        (
            "user-install-cmd",
            merged.user_install_commands,
            builtin.user_install_commands,
            "user_install_commands",
        ),
    ]
    for type_label, merged_entries, builtin_entries, config_attr in pools:
        if name not in merged_entries:
            continue

        entry = merged_entries[name]
        is_custom = name in getattr(config, config_attr, {})
        source = "custom" if is_custom else "built-in"
        overrides = name in builtin_entries and is_custom

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
            if entry.test_exec:
                typer.echo(f"Test exec:   {entry.test_exec}")
            if entry.test_file:
                typer.echo(f"Test file:   {entry.test_file}")
            if entry.test_dir:
                typer.echo(f"Test dir:    {entry.test_dir}")
            if entry.path:
                typer.echo(f"PATH:        {', '.join(entry.path)}")
        return

    typer.echo(f"Error: '{name}' not found in catalog", err=True)
    raise typer.Exit(1)


# -- Config commands -------------------------------------------------------


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
    db = _get_db()

    workspaces = db.list_workspaces(ws_type="vm")
    if not workspaces:
        typer.echo("No VM workspaces found.")
        return

    count = 0
    for ws in workspaces:
        if ws.vm_name is None:
            continue
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

    sync_ssh_config(load_config(), _get_db())



# -- Entrypoint ------------------------------------------------------------


def main() -> None:
    """CLI entrypoint. Wraps the typer app to catch ConfigError cleanly."""
    from agentworks.config import ConfigError

    try:
        app()
    except ConfigError as e:
        typer.echo(f"Configuration error: {e}", err=True)
        raise SystemExit(1) from None

