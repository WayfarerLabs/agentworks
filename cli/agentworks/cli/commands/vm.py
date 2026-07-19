"""`agentworks vm` -- manage virtual machines across declared vm-sites."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db

vm_app = typer.Typer(
    name="vm",
    help="Manage virtual machines.",
    no_args_is_help=True,
)
app.add_typer(vm_app)


@vm_app.command("create")
def vm_create(
    name: Annotated[str, typer.Argument(help="VM name")],
    template: Annotated[str | None, typer.Option("--template", help="VM template")] = None,
    site: Annotated[
        str | None,
        typer.Option(
            "--site",
            help=(
                "vm-site to create the VM at (a declared vm-site resource; "
                "falls back to defaults.site, else the single enabled "
                "site is inferred; when several are enabled, prompts for "
                "a choice)"
            ),
        ),
    ] = None,
) -> None:
    """Create a new VM (provision + initialize).

    Hardware (cpus, memory, disk, swap) and the admin username come from
    the selected vm-template and admin-template. To deviate, declare a
    new template rather than overriding on the command line.
    """
    from agentworks.config import load_config
    from agentworks.vms.manager import create_vm

    config = load_config()
    create_vm(
        get_db(),
        config,
        name=name,
        template=template,
        site=site,
    )


@vm_app.command("list")
def vm_list(
    names_only: Annotated[
        bool,
        typer.Option(
            "--names-only",
            help="Emit one VM name per line (no header, no formatting). "
            "Used by shell completion; the order matches the table's row order.",
        ),
    ] = False,
) -> None:
    """List VMs."""
    from agentworks.vms.manager import list_vms

    list_vms(get_db(), names_only=names_only)


@vm_app.command("backup")
def vm_backup(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Create a full backup of a VM: metadata, agents, workspaces, and files."""
    from agentworks.config import load_config
    from agentworks.vms.backup import backup_vm

    backup_vm(get_db(), load_config(), name)


@vm_app.command("describe")
def vm_describe(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Show detailed information about a VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import describe_vm

    describe_vm(get_db(), load_config(), name)


@vm_app.command("start")
def vm_start(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Start a stopped VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import start_vm

    start_vm(get_db(), load_config(), name)


@vm_app.command("stop")
def vm_stop(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Stop a running VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import stop_vm

    stop_vm(get_db(), load_config(), name)


@vm_app.command("delete")
def vm_delete(
    name: Annotated[str, typer.Argument(help="VM name")],
    force: Annotated[bool, typer.Option("--force", help="Force delete even with workspaces")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a VM and clean up all resources."""
    from agentworks.config import load_config
    from agentworks.vms.manager import delete_vm

    delete_vm(get_db(), load_config(), name, force=force, yes=yes)


@vm_app.command("rekey")
def vm_rekey(
    name: Annotated[str, typer.Argument(help="VM name")],
    wait_for_share: Annotated[
        bool, typer.Option("--wait-for-share", help="Wait for operator to share VM back to their tailnet")
    ] = False,
    ignore_env: Annotated[
        bool,
        typer.Option(
            "--ignore-env",
            help=(
                "Skip the env-var backend for the Tailscale auth-key secret "
                "and prompt for the new value. Masks AW_SECRET_TAILSCALE_AUTH_KEY "
                "(or the operator-typed backend_mappings.env-var override) so the "
                "resolver's prompt backend takes over."
            ),
        ),
    ] = False,
) -> None:
    """Assign a new Tailscale auth key to a VM (logout + rejoin)."""
    from agentworks.config import load_config
    from agentworks.vms.manager import rekey_vm

    rekey_vm(get_db(), load_config(), name, wait_for_share=wait_for_share, ignore_env=ignore_env)


@vm_app.command("reinit")
def vm_reinit(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Re-run initialization on a provisioned VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import reinit_vm

    reinit_vm(get_db(), load_config(), name)


@vm_app.command("exec", context_settings={"allow_extra_args": True, "allow_interspersed_args": False})
def vm_exec(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="VM name")],
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="Run from a workspace"),
    ] = None,
) -> None:
    """Execute a command on a VM as the admin user."""
    from agentworks.config import load_config
    from agentworks.vms.manager import exec_vm

    if not ctx.args:
        typer.echo("Error: missing command", err=True)
        raise typer.Exit(1)
    raise typer.Exit(
        exec_vm(get_db(), load_config(), name, ctx.args, workspace_name=workspace)
    )


@vm_app.command("shell")
def vm_shell(
    name: Annotated[str, typer.Argument(help="VM name")],
    platform: Annotated[
        bool,
        typer.Option(
            "--platform",
            # Legacy alias for one release. Visible in help: click has
            # no per-alias hiding.
            "--provisioner",
            help=(
                "Use the platform-native transport (limactl shell, wsl.exe, "
                "Azure public-IP SSH) instead of Tailscale SSH. Useful when "
                "Tailscale itself is the thing you're trying to reach the VM "
                "to fix."
            ),
        ),
    ] = False,
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", help="cd into a workspace"),
    ] = None,
) -> None:
    """Open a shell on a VM as the admin user."""
    from agentworks.config import load_config
    from agentworks.vms.manager import shell_vm

    raise typer.Exit(
        shell_vm(
            get_db(),
            load_config(),
            name,
            platform_transport=platform,
            workspace_name=workspace,
        )
    )


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

    raise typer.Exit(
        port_forward_vm(get_db(), load_config(), name, ports, address=address, verbose=verbose)
    )


@vm_app.command("add-git-credential")
def vm_add_git_credential(
    name: Annotated[str, typer.Argument(help="VM name")],
    credential: Annotated[str, typer.Argument(help="Git credential name from config")],
) -> None:
    """Add or update a git credential on a VM."""
    from agentworks.config import load_config
    from agentworks.vms.manager import add_git_credential

    add_git_credential(get_db(), load_config(), name, credential)


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

    Prefer 'agw console' for curated session lists and per-window shell panes.
    """
    from agentworks import output
    from agentworks.config import load_config
    from agentworks.sessions.console import attach_console

    output.warn(
        "'agw vm console' is deprecated; use 'agw console' "
        "(see 'agw console --help'). This command will be removed in a future release."
    )

    raise typer.Exit(
        attach_console(
            get_db(),
            load_config(),
            vm_name=name,
            recreate=recreate,
            allow_nesting=allow_nesting,
        )
    )
