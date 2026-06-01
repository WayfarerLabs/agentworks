"""Typer CLI entrypoint for Agentworks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Protocol

import click
import typer

from agentworks.db import Database, VMRow, WorkspaceRow

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

app = typer.Typer(
    name="agentworks",
    help="Orchestrate workspace lifecycle across multiple compute targets.",
    no_args_is_help=True,
    # Suppress typer's generic --install-completion / --show-completion flags
    # in favor of the project's hand-rolled `agentworks completion show|install`
    # subcommands, which emit scripts with the dynamic completers (vms,
    # workspaces, sessions, agents, consoles, ...).
    add_completion=False,
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

console_app = typer.Typer(
    name="console",
    help="Manage named consoles (curated tmux views of VM sessions).",
    no_args_is_help=True,
)
app.add_typer(console_app)

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
_debug = False


def _seed_debug_from_pre_callback() -> None:
    """Set ``_debug`` from sys.argv / AGW_DEBUG *before* Click parses anything.

    The typer callback below also sets ``_debug``, but it only fires after
    Click's own arg parsing succeeds. If the user passes ``--debug --bogus``,
    Click raises BadParameter before the callback ever runs -- so without
    this pre-pass, the user's ``--debug`` flag would be silently ineffective
    in exactly the case they're most likely to need it.
    """
    import os
    import sys

    global _debug  # noqa: PLW0603
    _debug = "--debug" in sys.argv or os.environ.get("AGW_DEBUG") == "1"


@app.callback()
def _global_options(
    non_interactive: Annotated[
        bool,
        typer.Option("--non-interactive", help="Disable interactive prompts"),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="Print full Python traceback on unhandled errors (also via AGW_DEBUG=1)",
        ),
    ] = False,
) -> None:
    """Global options for all commands."""
    import os

    global _non_interactive, _debug  # noqa: PLW0603
    _non_interactive = non_interactive
    _debug = debug or os.environ.get("AGW_DEBUG") == "1"


# -- Helpers ---------------------------------------------------------------


def _get_db() -> Database:
    return Database()


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


def _prompt_workspace(db: Database, workspace: str | None) -> WorkspaceRow:
    """Resolve a workspace, prompting if not provided and validating either way."""
    from agentworks import output

    if workspace is not None:
        ws = db.get_workspace(workspace)
        if ws is None:
            typer.echo(f"Error: workspace '{workspace}' not found.", err=True)
            raise typer.Exit(1)
        return ws

    workspaces = db.list_workspaces()
    if not workspaces:
        typer.echo("Error: no workspaces found. Create one with 'agentworks workspace create'.", err=True)
        raise typer.Exit(1)

    if len(workspaces) == 1:
        output.info(f"Using workspace '{workspaces[0].name}'")
        return workspaces[0]

    _require_interactive("--workspace")

    options = [f"{ws.name}  (vm: {ws.vm_name})" for ws in workspaces]
    idx = output.choose("Select a workspace:", options)
    return workspaces[idx]


def _prompt_vm(db: Database, vm_name: str | None) -> VMRow:
    """Resolve a VM, prompting if not provided and validating either way."""
    from agentworks import output

    if vm_name is not None:
        vm = db.get_vm(vm_name)
        if vm is None:
            typer.echo(f"Error: VM '{vm_name}' not found.", err=True)
            raise typer.Exit(1)
        return vm

    vms = db.list_vms()
    if not vms:
        typer.echo("Error: no VMs found. Create one with 'agentworks vm create'.", err=True)
        raise typer.Exit(1)

    if len(vms) == 1:
        output.info(f"Using VM '{vms[0].name}'")
        return vms[0]

    _require_interactive("--vm")

    options = [f"{v.name}  ({v.platform})" for v in vms]
    idx = output.choose("Select a VM:", options)
    return vms[idx]


class _HasDescription(Protocol):
    """Structural protocol for catalog entries that have a description."""

    @property
    def description(self) -> str: ...


# -- Top-level commands ----------------------------------------------------


completion_app = typer.Typer(
    name="completion",
    help="Generate or install shell completions.",
    no_args_is_help=True,
)
app.add_typer(completion_app)

# Accept the canonical `powershell` plus the `pwsh` alias users see in their
# binary name.
_SHELL_CHOICES = click.Choice(["bash", "zsh", "powershell", "pwsh"])


def _resolve_shell(shell: str | None) -> str:
    """Normalize and validate a --shell option, autodetecting if not given."""
    from agentworks.completions import detect_shell

    if shell is None:
        detected = detect_shell()
        if detected is None:
            typer.echo(
                "Error: unable to detect the shell. Pass --shell {bash|zsh|powershell}.",
                err=True,
            )
            raise typer.Exit(1)
        return detected
    if shell == "pwsh":
        return "powershell"
    return shell


@completion_app.command("show")
def completion_show(
    shell: Annotated[
        str | None,
        typer.Option("--shell", help="Shell type (autodetected if omitted)", click_type=_SHELL_CHOICES),
    ] = None,
) -> None:
    """Print the completion script to stdout."""
    from agentworks.completions import generate

    typer.echo(generate(_resolve_shell(shell)), nl=False)


@completion_app.command("install")
def completion_install(
    shell: Annotated[
        str | None,
        typer.Option("--shell", help="Shell type (autodetected if omitted)", click_type=_SHELL_CHOICES),
    ] = None,
) -> None:
    """Install the completion script to the appropriate location."""
    from agentworks.completions import generate
    from agentworks.completions.install import install_completions

    resolved = _resolve_shell(shell)
    install_completions(resolved, generate(resolved))


@app.command("doctor")
def doctor() -> None:
    """Check environment, config, and dependencies."""
    from agentworks.completions.spec import build_spec, completion_version
    from agentworks.doctor import Status, run_checks

    report = run_checks(completion_version=completion_version(build_spec(app)))

    typer.echo("Checking environment...\n")
    for group in report.groups:
        typer.echo(f"{group.name}:")
        for check in group.checks:
            label = {
                Status.OK: "[ok]",
                Status.INFO: "[info]",
                Status.WARN: "[warn]",
                Status.FAIL: "[FAIL]",
            }[check.status].ljust(6)
            msg = check.name
            if check.message is not None:
                msg += f" ({check.message})"
            typer.echo(f"  {label} {msg}")
        typer.echo()

    c = report.counts()
    typer.echo(
        f"Results: {c[Status.OK]} ok, {c[Status.INFO]} info, "
        f"{c[Status.WARN]} warn, {c[Status.FAIL]} fail"
    )
    if c[Status.FAIL] > 0:
        raise typer.Exit(1)


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
    name: Annotated[str, typer.Argument(help="VM name")],
    template: Annotated[str | None, typer.Option("--template", help="VM template")] = None,
    platform: Annotated[
        str | None,
        typer.Option("--platform", help="Platform", click_type=click.Choice(["lima", "azure", "wsl2", "proxmox"])),
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

    config = load_config()
    create_vm(
        _get_db(),
        config,
        name=name,
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


@vm_app.command("rekey")
def vm_rekey(
    name: Annotated[str, typer.Argument(help="VM name")],
    wait_for_share: Annotated[
        bool, typer.Option("--wait-for-share", help="Wait for operator to share VM back to their tailnet")
    ] = False,
    ignore_env: Annotated[
        bool, typer.Option("--ignore-env", help="Ignore TAILSCALE_AUTH_KEY env var and prompt for key")
    ] = False,
) -> None:
    """Assign a new Tailscale auth key to a VM (logout + rejoin)."""
    from agentworks.config import load_config
    from agentworks.vms.manager import rekey_vm

    rekey_vm(_get_db(), load_config(), name, wait_for_share=wait_for_share, ignore_env=ignore_env)


@vm_app.command("reinit")
def vm_reinit(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Re-run initialization on a provisioned VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import reinit_vm

    reinit_vm(_get_db(), load_config(), name)


@vm_app.command("exec", context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
def vm_exec(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Execute a command on a VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import exec_vm

    if not ctx.args:
        typer.echo("Error: missing command", err=True)
        raise typer.Exit(1)
    raise typer.Exit(exec_vm(_get_db(), load_config(), name, ctx.args))


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
    """[Deprecated] Attach to the VM console (creates it if needed).

    Prefer 'agentworks console' for curated session lists and per-window shell panes.
    """
    from agentworks import output
    from agentworks.config import load_config
    from agentworks.sessions.console import attach_console

    output.warn(
        "'agentworks vm console' is deprecated; use 'agentworks console' "
        "(see 'agentworks console --help'). This command will be removed in a future release."
    )

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
    name: Annotated[str, typer.Argument(help="Workspace name")],
    vm: Annotated[str | None, typer.Option("--vm", help="Target VM")] = None,
    template: Annotated[str | None, typer.Option("--template", help="Workspace template")] = None,
    open_vscode: Annotated[bool, typer.Option("--open-vscode", help="Open in VS Code")] = False,
) -> None:
    """Create a workspace on a VM."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import create_workspace

    db = _get_db()
    resolved_vm = _prompt_vm(db, vm)

    create_workspace(
        db,
        load_config(),
        name=name,
        vm_name=resolved_vm.name,
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
) -> None:
    """List workspaces."""
    from agentworks.workspaces.manager import list_workspaces

    list_workspaces(_get_db(), vm_name=vm)


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
    name: Annotated[str, typer.Argument(help="New workspace name")],
    vm: Annotated[str | None, typer.Option("--vm", help="Target VM")] = None,
) -> None:
    """Copy a workspace to a new VM workspace."""
    from agentworks.config import load_config
    from agentworks.workspaces.manager import copy_workspace

    copy_workspace(
        _get_db(),
        load_config(),
        source,
        dest_name=name,
        vm_name=vm,
    )


# -- Agent commands --------------------------------------------------------


@agent_app.command("create")
def agent_create(
    name: Annotated[str, typer.Argument(help="Agent name")],
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
    resolved_vm = _prompt_vm(db, vm)

    create_agent(
        db,
        load_config(),
        name=name,
        vm_name=resolved_vm.name,
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


@agent_app.command("exec", context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
def agent_exec(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Agent name")],
) -> None:
    """Execute a command as an agent user."""
    from agentworks.agents.manager import exec_agent
    from agentworks.config import load_config

    if not ctx.args:
        typer.echo("Error: missing command", err=True)
        raise typer.Exit(1)
    raise typer.Exit(exec_agent(_get_db(), load_config(), name=name, command=ctx.args))


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
    name: Annotated[str, typer.Argument(help="Session name")],
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
    new_agent: Annotated[bool, typer.Option("--new-agent", help="Create a new agent for this session")] = False,
    agent_name: Annotated[str | None, typer.Option("--agent-name", help="Name for new agent")] = None,
    agent_template: Annotated[
        str | None, typer.Option("--agent-template", help="Template for new agent")
    ] = None,
) -> None:
    """Create and start a session in a workspace."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import create_session
    from agentworks.workspaces.manager import create_workspace

    # Validate flag combinations before any prompts
    if admin and agent:
        typer.echo("Error: --admin and --agent are mutually exclusive", err=True)
        raise typer.Exit(1)
    if admin and new_agent:
        typer.echo("Error: --admin and --new-agent are mutually exclusive", err=True)
        raise typer.Exit(1)
    if agent and new_agent:
        typer.echo("Error: --agent and --new-agent are mutually exclusive", err=True)
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
    if not new_agent and (agent_name or agent_template):
        typer.echo(
            "Error: --agent-name and --agent-template require --new-agent",
            err=True,
        )
        raise typer.Exit(1)

    db = _get_db()
    config = load_config()

    resolved_vm: VMRow | None = None

    if new_workspace:
        resolved_vm = _prompt_vm(db, vm)
        resolved_workspace = workspace_name  # may be None, resolved after session name

        # Resolve mode (need VM name for agent lookup). Skip the prompt when
        # --new-agent is set -- the user has already chosen to create a new agent.
        resolved_agent: str | None = agent
        if not admin and agent is None and not new_agent:
            # Look up agents on the target VM
            vm_agents = db.list_agents(vm_name=resolved_vm.name)
            if vm_agents:
                _require_interactive("--admin or --agent")
                from agentworks import output

                options = ["admin"]
                for a in vm_agents:
                    label = f"agent: {a.name}"
                    if a.template:
                        label += f" [{a.template}]"
                    options.append(label)
                idx = output.choose("Run session as:", options)
                resolved_agent = None if idx == 0 else vm_agents[idx - 1].name

        resolved_ws_name = resolved_workspace or name

        create_workspace(
            db,
            config,
            name=resolved_ws_name,
            vm_name=resolved_vm.name,
            template_name=workspace_template,
        )
        resolved_workspace = resolved_ws_name
    else:
        ws = _prompt_workspace(db, workspace)
        resolved_workspace = ws.name

        # Resolve mode. Skip the prompt when --new-agent is set.
        resolved_agent: str | None = agent  # type: ignore[no-redef]
        if not admin and agent is None and not new_agent:
            resolved_agent = _prompt_session_mode(db, ws)

        if new_agent:
            # Agents are VM-scoped; pick up the workspace's VM.
            vm_row = db.get_vm(ws.vm_name)
            assert vm_row is not None  # FK guarantees existence
            resolved_vm = vm_row

    if new_agent:
        assert resolved_vm is not None
        from agentworks.agents.manager import create_agent

        resolved_agent_name = agent_name or name
        create_agent(
            db,
            config,
            name=resolved_agent_name,
            vm_name=resolved_vm.name,
            template=agent_template,
        )
        resolved_agent = resolved_agent_name

    create_session(
        db,
        config,
        name=name,
        workspace_name=resolved_workspace,
        template_name=template,
        agent_name=resolved_agent,
        created_workspace=new_workspace,
        created_agent=new_agent,
    )


def _prompt_session_mode(db: Database, ws: WorkspaceRow) -> str | None:
    """Prompt for admin vs agent mode. Returns agent name or None for admin."""
    from agentworks import output

    agents = db.list_agents(vm_name=ws.vm_name)
    if not agents:
        # No agents on this VM, default to admin
        return None

    _require_interactive("--admin or --agent")

    options = ["admin"]
    for a in agents:
        label = f"agent: {a.name}"
        if a.template:
            label += f" [{a.template}]"
        options.append(label)

    idx = output.choose("Run session as:", options)
    if idx == 0:
        return None
    return agents[idx - 1].name


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
    name: Annotated[str | None, typer.Argument(help="Session name")] = None,
    all_sessions: Annotated[bool, typer.Option("--all", help="Stop all running sessions")] = False,
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM (with --all)")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", help="Filter by workspace (with --all)")] = None,
    force: Annotated[bool, typer.Option("--force", help="Force-stop broken sessions via PID kill")] = False,
) -> None:
    """Stop a running session, or all running sessions with --all."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import stop_all_sessions, stop_session

    if name and all_sessions:
        raise typer.BadParameter("provide a session name or --all, not both")
    if (vm or workspace) and not all_sessions:
        raise typer.BadParameter("--vm and --workspace require --all")
    if all_sessions:
        stop_all_sessions(_get_db(), load_config(), vm_name=vm, workspace_name=workspace, force=force)
    elif name:
        stop_session(_get_db(), load_config(), name=name, force=force)
    else:
        raise typer.BadParameter("provide a session name or use --all")


@session_app.command("restart")
def session_restart(
    name: Annotated[str | None, typer.Argument(help="Session name")] = None,
    all_stopped: Annotated[bool, typer.Option("--all-stopped", help="Restart all stopped sessions")] = False,
    all_sessions: Annotated[bool, typer.Option("--all", help="Restart all sessions (prompts for running)")] = False,
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM (with --all/--all-stopped)")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace", help="Filter by workspace")] = None,
    force: Annotated[bool, typer.Option("--force", help="Force-kill broken sessions via PID")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompts")] = False,
) -> None:
    """Restart a session, or batch restart with --all-stopped / --all."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import restart_all_sessions, restart_session

    if name and (all_stopped or all_sessions):
        raise typer.BadParameter("provide a session name or a batch flag (--all/--all-stopped), not both")
    if all_stopped and all_sessions:
        raise typer.BadParameter("use --all or --all-stopped, not both")
    if (vm or workspace) and not (all_stopped or all_sessions):
        raise typer.BadParameter("--vm and --workspace require --all or --all-stopped")
    if all_stopped or all_sessions:
        db = _get_db()
        config = load_config()
        include_running = all_sessions

        # --all without --yes: prompt if there are running sessions
        if include_running and not yes:
            from agentworks import output
            from agentworks.sessions.manager import (
                batch_check_all_sessions,
                ensure_pids_batch,
                filter_sessions,
            )

            sessions = filter_sessions(db, workspace_name=workspace, vm_name=vm)
            sessions = ensure_pids_batch(sessions, db=db, config=config)
            from agentworks.db import SessionStatus

            status_map = batch_check_all_sessions(sessions, db=db, config=config)
            running = [s for s in sessions if status_map.get(s.name) == SessionStatus.OK]
            if running:
                names = ", ".join(s.name for s in running[:5])
                suffix = f" (and {len(running) - 5} more)" if len(running) > 5 else ""
                output.warn(
                    f"{len(running)} session(s) are running and will be restarted ({names}{suffix}).\n"
                    "Hint: use --all-stopped to restart only stopped sessions."
                )
                if not output.confirm("Continue?"):
                    raise output.UserAbort("restart cancelled")

        restart_all_sessions(
            db, config, vm_name=vm, workspace_name=workspace, include_running=include_running, force=force,
        )
    elif name:
        restart_session(_get_db(), load_config(), name=name, force=force, yes=yes)
    else:
        raise typer.BadParameter("provide a session name, --all-stopped, or --all")



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
    force: Annotated[bool, typer.Option("--force", help="Force-kill broken sessions via PID")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a session."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import delete_session

    delete_session(_get_db(), load_config(), name=name, force=force, yes=yes)


@session_app.command("logs")
def session_logs(
    name: Annotated[str, typer.Argument(help="Session name")],
    lines: Annotated[int | None, typer.Option("--lines", "-n", help="Number of lines")] = None,
) -> None:
    """Dump the scrollback buffer for a session."""
    from agentworks.config import load_config
    from agentworks.sessions.manager import session_logs as _session_logs

    _session_logs(_get_db(), load_config(), name=name, lines=lines)


# -- Console commands ------------------------------------------------------


@console_app.command("create")
def console_create(
    name: Annotated[str, typer.Argument(help="Console name")],
    sessions: Annotated[
        list[str] | None,
        typer.Argument(
            help="Sessions to include. Use 'name' or 'name+N' for N default shells.",
        ),
    ] = None,
    vm: Annotated[
        str | None,
        typer.Option(
            "--vm",
            help="Target VM (inferred from listed sessions; otherwise auto-picked or prompted)",
        ),
    ] = None,
    all_sessions: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Fill in every other session on the VM (0 shells each, alphabetical) after the explicit specs",
        ),
    ] = False,
    all_running: Annotated[
        bool,
        typer.Option(
            "--all-running",
            help=(
                "Like --all but only sessions whose live tmux state is OK "
                "(one SSH probe; VM must be reachable)"
            ),
        ),
    ] = False,
    add_admin_shell: Annotated[
        bool,
        typer.Option(
            "--add-admin-shell",
            help="Include a top-level admin-shell window (legacy vm-console behavior)",
        ),
    ] = False,
) -> None:
    """Create a named console with a curated set of sessions."""
    from agentworks.sessions.multi_console import (
        create_console,
        infer_vm_from_session_specs,
        parse_session_spec,
        running_session_names,
    )

    if all_sessions and all_running:
        raise typer.BadParameter("use --all or --all-running, not both")

    # Validate every spec up front so bad input (e.g. 'bad+nope') fails before
    # we prompt for a VM or hit the network on --all-running.
    specs = list(sessions or [])
    for s in specs:
        parse_session_spec(s)

    db = _get_db()
    # Resolve target VM:
    #   1. explicit --vm  -> validated by _prompt_vm
    #   2. inferred from listed sessions
    #   3. fall back to interactive/auto prompt
    if vm is None:
        vm = infer_vm_from_session_specs(db, specs)
    resolved_vm = _prompt_vm(db, vm)

    if all_running:
        # Live SSH probe (one round-trip per VM) so --all-running reflects
        # reality, not stale DB state. If the probe finds nothing and the
        # operator didn't list sessions or pass --add-admin-shell,
        # create_console below raises the canonical "empty console" error.
        from agentworks.config import load_config

        running = running_session_names(db, load_config(), resolved_vm.name)
        explicit_names = {parse_session_spec(s).name for s in specs}
        extras = [n for n in running if n not in explicit_names]
        specs.extend(extras)

    create_console(
        db,
        name=name,
        vm_name=resolved_vm.name,
        session_specs=specs,
        fill_all=all_sessions,
        add_admin_shell=add_admin_shell,
    )


@console_app.command("list")
def console_list(
    vm: Annotated[str | None, typer.Option("--vm", help="Filter by VM")] = None,
) -> None:
    """List consoles."""
    from agentworks.sessions.multi_console import list_consoles

    list_consoles(_get_db(), vm_name=vm)


@console_app.command("describe")
def console_describe(
    name: Annotated[str, typer.Argument(help="Console name")],
) -> None:
    """Show a console's membership and shell layout."""
    from agentworks.sessions.multi_console import describe_console

    describe_console(_get_db(), name=name)


@console_app.command("attach")
def console_attach(
    name: Annotated[str, typer.Argument(help="Console name")],
    recreate: Annotated[
        bool, typer.Option("--recreate", help="Kill and rebuild the console's tmux state")
    ] = False,
    allow_nesting: Annotated[
        bool, typer.Option("--allow-nesting", help="Allow attaching from inside an existing tmux")
    ] = False,
) -> None:
    """Attach to a named console (creating its tmux state on first attach)."""
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import attach_console

    attach_console(
        _get_db(),
        load_config(),
        name=name,
        recreate=recreate,
        allow_nesting=allow_nesting,
    )


@console_app.command("delete")
def console_delete(
    name: Annotated[str, typer.Argument(help="Console name")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
) -> None:
    """Delete a console: tear down its tmux session (best-effort) and remove its DB row."""
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import delete_console

    delete_console(_get_db(), load_config(), name=name, yes=yes)


@console_app.command("add-session")
def console_add_session(
    name: Annotated[str, typer.Argument(help="Console name")],
    sessions: Annotated[
        list[str],
        typer.Argument(help="Sessions to add. Use 'name' or 'name+N' for N default shells."),
    ],
) -> None:
    """Append sessions to an existing console."""
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import add_sessions

    add_sessions(_get_db(), load_config(), console_name=name, session_specs=sessions)


@console_app.command("remove-session")
def console_remove_session(
    name: Annotated[str, typer.Argument(help="Console name")],
    sessions: Annotated[list[str], typer.Argument(help="Session names to remove")],
) -> None:
    """Remove sessions from a console."""
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import remove_sessions

    remove_sessions(_get_db(), load_config(), console_name=name, session_names=sessions)


@console_app.command("add-shell")
def console_add_shell(
    name: Annotated[str, typer.Argument(help="Console name")],
    session: Annotated[str, typer.Argument(help="Session whose window gets the new pane")],
    cwd: Annotated[
        str | None,
        typer.Option("--cwd", help="Path relative to the workspace root (default = workspace root)"),
    ] = None,
    admin: Annotated[
        bool,
        typer.Option("--admin", help="Run shell as the VM admin user instead of the session's agent user"),
    ] = False,
) -> None:
    """Add a shell pane to a session window in a console."""
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import add_shell

    add_shell(
        _get_db(),
        load_config(),
        console_name=name,
        session_name=session,
        cwd=cwd,
        admin=admin,
    )


@console_app.command("restore-session")
def console_restore_session(
    name: Annotated[str, typer.Argument(help="Console name")],
    session: Annotated[str, typer.Argument(help="Session whose window to restore")],
) -> None:
    """Reconcile a session window's live shell panes against the configured list.

    Re-adds any panes you killed (e.g. accidentally), restoring each one to
    its original position. Refuses to remove panes if you have more live than
    configured; for that, use `console attach --recreate`. Consoles created
    before pane-tagging existed require `attach --recreate` once to retag from
    scratch.
    """
    from agentworks.config import load_config
    from agentworks.sessions.multi_console import restore_session

    restore_session(
        _get_db(),
        load_config(),
        console_name=name,
        session_name=session,
    )


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

    sync_ssh_config(load_config(), _get_db())



# -- Entrypoint ------------------------------------------------------------


def main() -> None:
    """CLI entrypoint. Sets up output handler and catches business logic errors."""
    import time

    from agentworks.errors import (
        AgentworksError,
        AlreadyExistsError,
        ConfigError,
        ConnectivityError,
        ExternalError,
        NotFoundError,
        StateError,
        UserAbort,
        ValidationError,
    )
    from agentworks.output import Progress, set_handler

    # -- Typer output handler --------------------------------------------------

    class _TyperProgress:
        def __init__(self, label: str, total: int | None = None) -> None:
            self._label = label
            self._total = total
            self._start = time.monotonic()

        def update(self, current: int | None = None, message: str | None = None) -> None:
            parts = [f"  {self._label}..."]
            if current is not None and self._total is not None and self._total > 0:
                pct = current / self._total * 100
                parts.append(f" {pct:.0f}% ({current}/{self._total})")
            if message:
                parts.append(f" {message}")
            typer.echo("".join(parts))

        def done(self, message: str | None = None) -> None:
            elapsed = time.monotonic() - self._start
            suffix = f" {message}" if message else ""
            typer.echo(f"  {self._label} done ({elapsed:.0f}s){suffix}")

    class _TyperHandler:
        def info(self, message: str) -> None:
            typer.echo(message)

        def detail(self, message: str, indent: int = 1) -> None:
            typer.echo(f"{'  ' * indent}{message}")

        def warn(self, message: str) -> None:
            typer.echo(f"Warning: {message}", err=True)

        def confirm(self, message: str, default: bool = False) -> bool:
            try:
                return typer.confirm(message, default=default)
            except click.exceptions.Abort:
                from agentworks.output import UserAbort

                raise UserAbort("interrupted") from None

        def choose(self, message: str, options: list[str]) -> int:
            typer.echo(message)
            for i, option in enumerate(options, 1):
                typer.echo(f"  {i}) {option}")
            while True:
                try:
                    choice = int(typer.prompt("Choice", type=int))
                    if 1 <= choice <= len(options):
                        return choice - 1
                except click.exceptions.Abort:
                    from agentworks.output import UserAbort

                    raise UserAbort("interrupted") from None
                except ValueError:
                    pass
                typer.echo(f"Invalid choice. Enter 1-{len(options)}.")

        def pause(self, message: str) -> None:
            try:
                input(message)
            except (EOFError, KeyboardInterrupt):
                from agentworks.output import UserAbort

                raise UserAbort("interrupted") from None

        def prompt(self, label: str, default: str | None = None) -> str:
            return str(typer.prompt(label, default=default or ""))

        def prompt_secret(self, label: str, hint: str | None = None) -> str:
            import click

            try:
                if hint:
                    typer.echo(f"  {hint}", err=True)
                while True:
                    value = str(click.prompt(label, err=True, default="", hide_input=True))
                    if value.strip():
                        break
                    typer.echo("(empty, try again)", err=True)
                return value
            except click.exceptions.Abort:
                from agentworks.output import UserAbort

                raise UserAbort("interrupted") from None

        def progress(self, label: str, total: int | None = None) -> Progress:
            typer.echo(f"  {label}...")
            return _TyperProgress(label, total)

    set_handler(_TyperHandler())

    # -- Run app ---------------------------------------------------------------

    try:
        # Set _debug from sys.argv/env *before* Click parses anything, so a
        # framework-level parse error (e.g. --debug --bogus) still honors the
        # flag. The typer callback re-sets _debug after Click parses
        # successfully. Inside the try so a Ctrl-C during the pre-pass still
        # routes through our wrapper.
        _seed_debug_from_pre_callback()
        app()
    except ConfigError as e:
        # Config errors get their own label since the user is looking at the
        # wrong file, not at a runtime state problem.
        typer.echo(f"Configuration error: {e}", err=True)
        _echo_hint(e)
        raise SystemExit(1) from None
    except UserAbort:
        typer.echo("Aborted.", err=True)
        raise SystemExit(1) from None
    except (NotFoundError, AlreadyExistsError, ValidationError, StateError) as e:
        # Clean domain errors: render as a one-liner with no traceback. These
        # are user-facing and a traceback adds noise without diagnostic value.
        typer.echo(f"Error: {e}", err=True)
        _echo_hint(e)
        raise SystemExit(1) from None
    except (ConnectivityError, ExternalError) as e:
        # External-system failures: render the one-liner AND persist the
        # full traceback to the error log so postmortem diagnosis can see
        # the underlying SSH command, provisioner response, etc. Type-qualify
        # the message (Error: SSHError: ...) since these often have messages
        # that don't carry the failure category in their text.
        typer.echo(f"Error: {type(e).__name__}: {e}", err=True)
        _echo_hint(e)
        if _debug:
            raise
        log_path = _record_unhandled_error(e)
        if log_path is not None:
            typer.echo(
                f"(full traceback written to {log_path}; "
                f"rerun with --debug or AGW_DEBUG=1 to print on stderr)",
                err=True,
            )
        else:
            typer.echo(
                "(could not write traceback to log; "
                "rerun with --debug or AGW_DEBUG=1 to print on stderr)",
                err=True,
            )
        raise SystemExit(1) from None
    except AgentworksError as e:
        # Catch-all for AgentworksError subclasses not handled above. In PR A,
        # this covers the deprecated by-manager classes (VMError, WorkspaceError,
        # AgentError, SessionError, ConsoleError) that still directly extend
        # AgentworksError. PR B will reclassify their raise sites and this
        # catch will go away.
        typer.echo(f"Error: {e}", err=True)
        _echo_hint(e)
        raise SystemExit(1) from None
    except (click.exceptions.ClickException, click.exceptions.Exit, click.exceptions.Abort):
        # Let Click / Typer own their own rendering and exit codes. Typer
        # converts KeyboardInterrupt to click.Exit(130) internally before this
        # try block sees it (see typer/core.py), so ctrl-C is already handled
        # silently with the conventional SIGINT exit code; per-op rollback
        # handlers fire inside the command, before typer's conversion.
        raise
    except KeyboardInterrupt:
        # Defensive: a KI that somehow bypasses typer's internal conversion
        # (e.g. raised during main()'s own setup, before app() runs).
        typer.echo("Cancelled.", err=True)
        raise SystemExit(130) from None
    except Exception as e:
        # Anything else is an unhandled error (third-party library, internal
        # bug, OSError, etc.). Print a clean one-liner, persist the full
        # traceback to the error log for post-hoc debugging, and exit non-zero.
        # Re-raise under --debug / AGW_DEBUG=1 so devs/CI see the traceback.
        if _debug:
            raise
        log_path = _record_unhandled_error(e)
        typer.echo(f"Error: {type(e).__name__}: {e}", err=True)
        if log_path is not None:
            typer.echo(
                f"(full traceback written to {log_path}; "
                f"rerun with --debug or AGW_DEBUG=1 to print on stderr)",
                err=True,
            )
        else:
            typer.echo(
                "(could not write traceback to log; "
                "rerun with --debug or AGW_DEBUG=1 to print on stderr)",
                err=True,
            )
        raise SystemExit(1) from None


def _echo_hint(exc: BaseException) -> None:
    """Render an AgentworksError's hint attribute on a second line if set."""
    hint = getattr(exc, "hint", None)
    if hint:
        typer.echo(f"  Hint: {hint}", err=True)


def _record_unhandled_error(exc: BaseException) -> Path | None:
    """Append the traceback + invocation context to the error log. Best-effort.

    Returns the log path on success, or None if writing failed (the user's
    one-line error message takes precedence over the persisted traceback).

    The log appends forever -- not currently rotated. Errors are rare; a few
    MB takes years to accumulate. Add rotation later if it becomes an issue.
    """
    import datetime
    import shlex
    import sys
    import traceback

    from agentworks.config import CONFIG_DIR

    log_dir = CONFIG_DIR / "logs"
    log_path = log_dir / "error.log"

    ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    argv = shlex.join(sys.argv)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 72}\n")
            f.write(f"{ts}\n")
            f.write(f"argv: {argv}\n\n")
            f.write(tb)
    except OSError:
        return None
    return log_path

