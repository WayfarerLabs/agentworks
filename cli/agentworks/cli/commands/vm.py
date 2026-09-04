"""`agentworks vm` -- manage virtual machines across declared vm-sites."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agentworks.cli._app import app
from agentworks.cli._helpers import get_db, ordinary_tty_interaction_policy
from agentworks.machine_output import OutputFormat

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
    spec: Annotated[
        str | None,
        typer.Option("--spec", help="Inline JSON VM spec applied after the selected VM template"),
    ] = None,
    admin_template: Annotated[
        str | None,
        typer.Option(
            "--admin-template",
            help=(
                "admin-template to provision the VM's admin user from (a "
                "declared admin-template resource; default: the 'default' "
                "admin-template)"
            ),
        ),
    ] = None,
    admin_spec: Annotated[
        str | None,
        typer.Option(
            "--admin-spec",
            help="Inline JSON admin spec applied after the selected admin template",
        ),
    ] = None,
    site: Annotated[
        str | None,
        typer.Option(
            "--site",
            help=(
                "vm-site to create the VM at (a declared vm-site resource; "
                "falls back to defaults.site, else the single ready "
                "site is inferred; when several are ready, prompts for "
                "a choice)"
            ),
        ),
    ] = None,
) -> None:
    """Create a new VM (provision + initialize).

    Hardware starts with the selected vm-template and the admin user starts
    with the selected admin-template. The two inline spec options apply the
    final VM-specific layers after those respective templates.
    """
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.manager import create_vm

    config = load_config()
    create_vm(
        get_db(),
        config,
        name=name,
        template=template,
        spec=spec,
        admin_template=admin_template,
        admin_spec=admin_spec,
        site=site,
        interaction=interaction,
    )


@vm_app.command("list")
def vm_list(
    status: Annotated[bool, typer.Option("--status", help="Include live runtime status")] = False,
    names_only: Annotated[
        bool,
        typer.Option(
            "--names-only",
            help="Emit one VM name per line (no header, no formatting). "
            "Used by shell completion; the order matches the table's row order.",
        ),
    ] = False,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json. Default: human."),
    ] = OutputFormat.HUMAN,
) -> None:
    """List VMs."""
    if names_only and output_format is OutputFormat.JSON:
        raise typer.BadParameter("cannot be used with --output json", param_hint="--names-only")
    if status and names_only:
        raise typer.BadParameter("cannot be used with --names-only", param_hint="--status")

    from agentworks.vms.manager import list_vms, render_vm_listing, vm_listing

    if names_only:
        list_vms(get_db(), names_only=True)
        return

    config = None
    interaction = None
    if status:
        from agentworks.config import load_config

        config = load_config(warn_issues=output_format is OutputFormat.HUMAN)
        interaction = ordinary_tty_interaction_policy()
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks import output
        from agentworks.machine_output import MachineOutputCommand, write_json_envelope
        from agentworks.vms.manager.inspect import vm_listing_data

        with output.suppress_presentation():
            listing = vm_listing(
                get_db(),
                config,
                include_status=status,
                interaction=interaction,
            )
        write_json_envelope(
            MachineOutputCommand.VM_LIST,
            vm_listing_data(listing),
            get_binary_stream("stdout"),
        )
        return
    listing = vm_listing(
        get_db(),
        config,
        include_status=status,
        interaction=interaction,
    )
    render_vm_listing(listing, names_only=names_only, include_status=status)


@vm_app.command("backup")
def vm_backup(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Create a full backup of a VM: metadata, agents, workspaces, and files."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.backup import backup_vm

    backup_vm(get_db(), load_config(), name, interaction=interaction)


@vm_app.command("confirm-release")
def vm_confirm_release(
    name: Annotated[str, typer.Argument(help="VM name")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Observe and explicitly record a VM's live Debian release."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.manager import confirm_vm_release

    confirm_vm_release(get_db(), load_config(), name, yes=yes, interaction=interaction)


@vm_app.command("describe")
def vm_describe(
    name: Annotated[str, typer.Argument(help="VM name")],
    output_format: Annotated[
        OutputFormat,
        typer.Option("--output", help="Output format: human or json. Default: human."),
    ] = OutputFormat.HUMAN,
) -> None:
    """Show detailed information about a VM."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.manager import describe_vm, vm_description

    config = load_config(warn_issues=output_format is OutputFormat.HUMAN)
    if output_format is OutputFormat.JSON:
        from click import get_binary_stream

        from agentworks import output
        from agentworks.machine_output import MachineOutputCommand, write_json_envelope
        from agentworks.vms.manager.inspect import vm_description_data

        with output.suppress_presentation():
            description = vm_description(get_db(), config, name, interaction=interaction)
        write_json_envelope(
            MachineOutputCommand.VM_DESCRIBE,
            vm_description_data(description),
            get_binary_stream("stdout"),
        )
        return
    describe_vm(get_db(), config, name, interaction=interaction)


@vm_app.command("verify-connection")
def vm_verify_connection(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Verify the canonical admin connection without activating the VM."""
    from agentworks import output
    from agentworks.bootstrap import load_request_registry
    from agentworks.config import load_config
    from agentworks.vms.manager import verify_vm_connection

    config = load_config()
    db = get_db()
    registry = load_request_registry(config, live_database=db)
    result = verify_vm_connection(db, config, registry, name)
    output.result(f"VM '{result.name}' connection verified via {result.transport}.")


@vm_app.command("start")
def vm_start(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Start a stopped VM."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.manager import start_vm

    start_vm(get_db(), load_config(), name, interaction=interaction)


@vm_app.command("stop")
def vm_stop(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Stop a running VM."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.manager import stop_vm

    stop_vm(get_db(), load_config(), name, interaction=interaction)


@vm_app.command("delete")
def vm_delete(
    name: Annotated[str, typer.Argument(help="VM name")],
    force: Annotated[bool, typer.Option("--force", help="Force delete even with workspaces")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Delete a VM and clean up all resources."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.manager import delete_vm

    delete_vm(
        get_db(),
        load_config(),
        name,
        force=force,
        yes=yes,
        interaction=interaction,
    )


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
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.manager import rekey_vm

    rekey_vm(
        get_db(),
        load_config(),
        name,
        wait_for_share=wait_for_share,
        ignore_env=ignore_env,
        interaction=interaction,
    )


@vm_app.command("reinit")
def vm_reinit(
    name: Annotated[str, typer.Argument(help="VM name")],
) -> None:
    """Re-run initialization on a provisioned VM."""
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.manager import reinit_vm

    reinit_vm(get_db(), load_config(), name, interaction=interaction)


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
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.manager import exec_vm

    if not ctx.args:
        typer.echo("Error: missing command", err=True)
        raise typer.Exit(1)
    raise typer.Exit(
        exec_vm(
            get_db(),
            load_config(),
            name,
            ctx.args,
            workspace_name=workspace,
            interaction=interaction,
        )
    )


@vm_app.command("shell")
def vm_shell(
    name: Annotated[str, typer.Argument(help="VM name")],
    platform: Annotated[
        bool,
        typer.Option(
            "--platform",
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
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.manager import shell_vm

    raise typer.Exit(
        shell_vm(
            get_db(),
            load_config(),
            name,
            platform_transport=platform,
            workspace_name=workspace,
            interaction=interaction,
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
    interaction = ordinary_tty_interaction_policy()
    from agentworks.config import load_config
    from agentworks.vms.manager import port_forward_vm

    raise typer.Exit(
        port_forward_vm(
            get_db(),
            load_config(),
            name,
            ports,
            address=address,
            verbose=verbose,
            interaction=interaction,
        )
    )


@vm_app.command("logs")
def vm_logs(
    name: Annotated[str, typer.Argument(help="VM name")],
    show_all: Annotated[bool, typer.Option("--all", help="Show all logs instead of only the latest")] = False,
) -> None:
    """Show SSH logs for a VM."""
    from agentworks import output
    from agentworks.ssh import LOG_DIR

    if not LOG_DIR.exists():
        output.info("No logs found.")
        return

    # Collect all logs for this VM -- filename is <vm>-<timestamp>-<cmd>.log
    all_logs = sorted(LOG_DIR.glob(f"{name}-*.log"), reverse=True)
    logs = [(str(p), p.name) for p in all_logs]

    if not logs:
        output.info(f"No SSH logs found for VM '{name}'.")
        return

    # Header names the log file (the handler owns any decoration); the raw
    # log body is emitted flush-left so it stays copy-paste faithful.
    display = logs if show_all else logs[:1]
    for log_path, log_name in display:
        output.info(log_name)
        output.info(Path(log_path).read_text().rstrip("\n"))
        output.info("")
