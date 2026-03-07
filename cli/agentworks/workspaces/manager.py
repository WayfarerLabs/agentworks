"""Workspace lifecycle orchestration."""

from __future__ import annotations

import secrets
import subprocess
from typing import TYPE_CHECKING

import typer

from agentworks.config import NAME_RE
from agentworks.workspaces.templates import resolve_template

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow


def _generate_name() -> str:
    return secrets.token_hex(4)[:7]


def create_workspace(
    db: Database,
    config: Config,
    *,
    name: str | None = None,
    vm_name: str | None = None,
    template_name: str | None = None,
    open_vscode: bool = False,
) -> None:
    """Create a workspace on a VM."""
    ws_name = name or _generate_name()
    if not NAME_RE.match(ws_name):
        typer.echo(f"Error: invalid name '{ws_name}'. Must match [a-z0-9\\-_.]", err=True)
        raise typer.Exit(1)

    if db.get_workspace(ws_name) is not None:
        typer.echo(f"Error: workspace '{ws_name}' already exists", err=True)
        raise typer.Exit(1)

    # Resolve VM
    vm = _resolve_vm(db, vm_name)

    # Check VM is ready
    if vm.init_status != "complete":
        typer.echo(
            f"Error: VM '{vm.name}' initialization is not complete (status: {vm.init_status}). "
            "Run 'vm delete' and recreate, or SSH in manually to debug.",
            err=True,
        )
        raise typer.Exit(1)

    # Auto-start if needed
    _ensure_vm_running(db, config, vm)

    # Resolve template
    template = resolve_template(config, template_name)
    typer.echo(f"Creating workspace '{ws_name}' on VM '{vm.name}' (template: {template.name})...")

    # Create on VM
    from agentworks.workspaces.backends.vm import create_vm_workspace, generate_code_workspace

    workspace_path = create_vm_workspace(vm, config, ws_name, template)

    # Generate .code-workspace
    code_ws_path = generate_code_workspace(vm, config, ws_name, workspace_path)
    typer.echo(f"VS Code workspace: {code_ws_path}")

    # Record in DB
    db.insert_workspace(ws_name, ws_type="vm", workspace_path=workspace_path, vm_name=vm.name, template=template.name)

    # Open VS Code if requested
    if open_vscode:
        subprocess.run(["code", code_ws_path], check=False)

    typer.echo(f"Workspace '{ws_name}' created")


def shell_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    no_tmuxinator: bool = False,
) -> None:
    """Open a shell into a workspace."""
    ws = db.get_workspace(name)
    if ws is None:
        typer.echo(f"Error: workspace '{name}' not found", err=True)
        raise typer.Exit(1)

    if ws.type == "vm":
        vm = db.get_vm(ws.vm_name)  # type: ignore[arg-type]
        if vm is None:
            typer.echo(f"Error: VM '{ws.vm_name}' not found", err=True)
            raise typer.Exit(1)

        if vm.init_status != "complete":
            typer.echo(f"Error: VM '{vm.name}' initialization is not complete", err=True)
            raise typer.Exit(1)

        _ensure_vm_running(db, config, vm)
        db.update_workspace_last_seen(name)

        # Check if template had tmuxinator enabled
        template = resolve_template(config, ws.template)
        use_tmux = template.tmuxinator and not no_tmuxinator

        from agentworks.workspaces.backends.vm import shell_vm_workspace

        shell_vm_workspace(
            vm, config, name, ws.workspace_path,
            use_tmuxinator=use_tmux,
            tmuxinator_enabled=template.tmuxinator,
        )
    else:
        typer.echo("Error: local workspaces not yet implemented", err=True)
        raise typer.Exit(1)


def list_workspaces(
    db: Database,
    *,
    vm_name: str | None = None,
    ws_type: str | None = None,
) -> None:
    """List workspaces."""
    workspaces = db.list_workspaces(vm_name=vm_name, ws_type=ws_type)
    if not workspaces:
        typer.echo("No workspaces found.")
        return

    typer.echo(f"{'NAME':<20} {'TYPE':<8} {'VM':<15} {'TEMPLATE':<15} {'CREATED'}")
    typer.echo("-" * 80)
    for ws in workspaces:
        typer.echo(
            f"{ws.name:<20} {ws.type:<8} {ws.vm_name or '-':<15} "
            f"{ws.template or '-':<15} {ws.created_at}"
        )


def delete_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    yes: bool = False,
) -> None:
    """Delete a workspace."""
    ws = db.get_workspace(name)
    if ws is None:
        typer.echo(f"Error: workspace '{name}' not found", err=True)
        raise typer.Exit(1)

    if not yes:
        typer.confirm(f"Delete workspace '{name}'?", abort=True)

    if ws.type == "vm" and ws.vm_name:
        vm = db.get_vm(ws.vm_name)
        if vm is not None:
            from agentworks.workspaces.backends.vm import delete_vm_workspace

            delete_vm_workspace(vm, config, name, ws.workspace_path)

    # Remove .code-workspace file
    code_ws_path = config.paths.code_workspaces / f"{name}.code-workspace"
    code_ws_path.unlink(missing_ok=True)

    db.delete_workspace(name)
    typer.echo(f"Workspace '{name}' deleted")


def _resolve_vm(db: Database, vm_name: str | None) -> VMRow:
    """Resolve which VM to use: explicit, auto-select if 1, or error."""
    if vm_name is not None:
        vm = db.get_vm(vm_name)
        if vm is None:
            typer.echo(f"Error: VM '{vm_name}' not found", err=True)
            raise typer.Exit(1)
        return vm

    vms = db.list_vms()
    complete_vms = [v for v in vms if v.init_status == "complete"]

    if len(complete_vms) == 0:
        typer.echo("Error: no VMs available. Create one with 'agentworks vm create'.", err=True)
        raise typer.Exit(1)

    if len(complete_vms) == 1:
        return complete_vms[0]

    typer.echo("Error: multiple VMs available. Specify --vm:", err=True)
    for v in complete_vms:
        typer.echo(f"  {v.name}", err=True)
    raise typer.Exit(1)


def _ensure_vm_running(db: Database, config: Config, vm: VMRow) -> None:
    """Auto-start a stopped/deallocated VM."""
    from agentworks.vms.manager import get_provisioner

    vm_host_ssh: str | None = None
    if vm.vm_host_name:
        host = db.get_vm_host(vm.vm_host_name)
        if host:
            vm_host_ssh = host.ssh_host

    provisioner = get_provisioner(vm.platform, vm_host_ssh)
    status = provisioner.status(vm)

    if status.value in ("stopped", "deallocated"):
        typer.echo(f"VM '{vm.name}' is {status.value}. Starting...")
        provisioner.start(vm)
        typer.echo(f"VM '{vm.name}' started")
