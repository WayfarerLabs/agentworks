"""Workspace lifecycle orchestration."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import typer

from agentworks.config import NAME_RE
from agentworks.workspaces.templates import resolve_template

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow


def create_workspace(
    db: Database,
    config: Config,
    *,
    name: str,
    vm_name: str | None = None,
    local: bool = False,
    template_name: str | None = None,
    open_vscode: bool = False,
) -> None:
    """Create a workspace on a VM or locally."""
    ws_name = name
    if not NAME_RE.match(ws_name):
        typer.echo(f"Error: invalid name '{ws_name}'. Must match [a-z0-9\\-_.]", err=True)
        raise typer.Exit(1)

    if db.get_workspace(ws_name) is not None:
        typer.echo(f"Error: workspace '{ws_name}' already exists", err=True)
        raise typer.Exit(1)

    # Resolve template
    template = resolve_template(config, template_name)

    if local:
        _create_local(db, config, ws_name, template_name=template.name, template=template, open_vscode=open_vscode)
    else:
        _create_vm(
            db, config, ws_name,
            vm_name=vm_name, template_name=template.name,
            template=template, open_vscode=open_vscode,
        )


def _create_local(
    db: Database,
    config: Config,
    ws_name: str,
    *,
    template_name: str,
    template: object,
    open_vscode: bool,
) -> None:
    from agentworks.workspaces.backends.local import create_local_workspace
    from agentworks.workspaces.templates import ResolvedTemplate

    assert isinstance(template, ResolvedTemplate)

    typer.echo(f"Creating local workspace '{ws_name}' (template: {template_name})...")
    workspace_path = create_local_workspace(config, ws_name, template)

    db.insert_workspace(ws_name, ws_type="local", workspace_path=workspace_path, template=template_name)

    if open_vscode:
        subprocess.run(["code", workspace_path], check=False)

    typer.echo(f"Workspace '{ws_name}' created at {workspace_path}")


def _create_vm(
    db: Database,
    config: Config,
    ws_name: str,
    *,
    vm_name: str | None,
    template_name: str,
    template: object,
    open_vscode: bool,
) -> None:
    from agentworks.workspaces.backends.vm import create_vm_workspace, generate_code_workspace
    from agentworks.workspaces.templates import ResolvedTemplate

    assert isinstance(template, ResolvedTemplate)

    vm = _resolve_vm(db, vm_name)

    if vm.init_status != "complete":
        typer.echo(
            f"Error: VM '{vm.name}' initialization is not complete (status: {vm.init_status}). "
            "Run 'vm delete' and recreate, or SSH in manually to debug.",
            err=True,
        )
        raise typer.Exit(1)

    _ensure_vm_running(db, config, vm)

    typer.echo(f"Creating workspace '{ws_name}' on VM '{vm.name}' (template: {template_name})...")
    workspace_path = create_vm_workspace(vm, config, ws_name, template)

    code_ws_path = generate_code_workspace(vm, config, ws_name, workspace_path)
    typer.echo(f"VS Code workspace: {code_ws_path}")

    db.insert_workspace(
        ws_name, ws_type="vm", workspace_path=workspace_path,
        vm_name=vm.name, template=template_name,
    )

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

    template = resolve_template(config, ws.template)
    use_tmux = template.tmuxinator and not no_tmuxinator

    if ws.type == "local":
        from agentworks.workspaces.backends.local import shell_local_workspace

        db.update_workspace_last_seen(name)
        shell_local_workspace(
            name, ws.workspace_path,
            use_tmuxinator=use_tmux,
            tmuxinator_enabled=template.tmuxinator,
        )
    elif ws.type == "vm":
        vm = db.get_vm(ws.vm_name)  # type: ignore[arg-type]
        if vm is None:
            typer.echo(f"Error: VM '{ws.vm_name}' not found", err=True)
            raise typer.Exit(1)

        if vm.init_status != "complete":
            typer.echo(f"Error: VM '{vm.name}' initialization is not complete", err=True)
            raise typer.Exit(1)

        _ensure_vm_running(db, config, vm)
        db.update_workspace_last_seen(name)

        from agentworks.workspaces.backends.vm import shell_vm_workspace

        shell_vm_workspace(
            vm, config, name, ws.workspace_path,
            use_tmuxinator=use_tmux,
            tmuxinator_enabled=template.tmuxinator,
        )
    else:
        typer.echo(f"Error: unknown workspace type '{ws.type}'", err=True)
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

    if ws.type == "local":
        from agentworks.workspaces.backends.local import delete_local_workspace

        delete_local_workspace(name, ws.workspace_path)
    elif ws.type == "vm" and ws.vm_name:
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
