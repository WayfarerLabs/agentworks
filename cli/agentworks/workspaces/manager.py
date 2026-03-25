"""Workspace lifecycle orchestration."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import typer

from agentworks.config import validate_name
from agentworks.db import InitStatus, VMStatus
from agentworks.workspaces.templates import ResolvedTemplate, resolve_template

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
    validate_name(ws_name)

    if db.get_workspace(ws_name) is not None:
        typer.echo(f"Error: workspace '{ws_name}' already exists", err=True)
        raise typer.Exit(1)

    # Resolve template
    template = resolve_template(config, template_name)

    if local:
        _create_local(db, config, ws_name, template_name=template.name, template=template, open_vscode=open_vscode)
    else:
        _create_vm(
            db,
            config,
            ws_name,
            vm_name=vm_name,
            template_name=template.name,
            template=template,
            open_vscode=open_vscode,
        )


def _create_local(
    db: Database,
    config: Config,
    ws_name: str,
    *,
    template_name: str,
    template: ResolvedTemplate,
    open_vscode: bool,
) -> None:
    from agentworks.workspaces.backends.local import create_local_workspace, delete_local_workspace

    workspace_path: str | None = None
    try:
        typer.echo(f"Creating local workspace '{ws_name}' (template: {template_name})...")
        workspace_path = create_local_workspace(config, ws_name, template)

        db.insert_workspace(ws_name, ws_type="local", workspace_path=workspace_path, template=template_name)
    except SystemExit:
        # typer.Exit -- already reported, just clean up
        if workspace_path:
            delete_local_workspace(ws_name, workspace_path)
        raise
    except Exception as e:
        typer.echo(f"Error creating workspace: {e}", err=True)
        if workspace_path:
            delete_local_workspace(ws_name, workspace_path)
        raise typer.Exit(1) from None

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
    template: ResolvedTemplate,
    open_vscode: bool,
) -> None:
    from agentworks.workspaces.backends.vm import (
        create_vm_workspace,
        delete_vm_workspace,
        generate_code_workspace,
    )

    vm = _resolve_vm(db, vm_name)

    _guard_vm_status(vm)

    _ensure_vm_running(db, config, vm)

    workspace_path: str | None = None
    code_ws_path: str | None = None

    def _cleanup() -> None:
        if workspace_path:
            delete_vm_workspace(vm, config, ws_name, workspace_path)
        if code_ws_path:
            from pathlib import Path

            Path(code_ws_path).unlink(missing_ok=True)

    try:
        typer.echo(f"Creating workspace '{ws_name}' on VM '{vm.name}' (template: {template_name})...")
        workspace_path = create_vm_workspace(vm, config, ws_name, template)

        code_ws_path = generate_code_workspace(vm, config, ws_name, workspace_path)
        typer.echo(f"VS Code workspace: {code_ws_path}")

        db.insert_workspace(
            ws_name,
            ws_type="vm",
            workspace_path=workspace_path,
            vm_name=vm.name,
            template=template_name,
        )
    except SystemExit:
        _cleanup()
        raise
    except Exception as e:
        typer.echo(f"Error creating workspace: {e}", err=True)
        _cleanup()
        raise typer.Exit(1) from None

    if open_vscode:
        subprocess.run(["code", code_ws_path], check=False)

    typer.echo(f"Workspace '{ws_name}' created")


def shell_workspace(
    db: Database,
    config: Config,
    name: str,
) -> None:
    """Open a plain shell into a workspace."""
    ws = db.get_workspace(name)
    if ws is None:
        typer.echo(f"Error: workspace '{name}' not found", err=True)
        raise typer.Exit(1)

    if ws.type == "local":
        from agentworks.workspaces.backends.local import shell_local_workspace

        db.update_workspace_last_seen(name)
        shell_local_workspace(ws.workspace_path)
    elif ws.type == "vm":
        vm = db.get_vm(ws.vm_name)  # type: ignore[arg-type]
        if vm is None:
            typer.echo(f"Error: VM '{ws.vm_name}' not found", err=True)
            raise typer.Exit(1)

        _guard_vm_status(vm)
        _ensure_vm_running(db, config, vm)
        db.update_workspace_last_seen(name)

        from agentworks.workspaces.backends.vm import shell_vm_workspace

        shell_vm_workspace(vm, config, ws.workspace_path)
    else:
        typer.echo(f"Error: unknown workspace type '{ws.type}'", err=True)
        raise typer.Exit(1)


def console_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    allow_nesting: bool = False,
    recreate: bool = False,
) -> None:
    """Open the workspace console (tmuxinator session with tasks)."""
    import os

    if os.environ.get("TMUX") and not allow_nesting:
        typer.echo(
            "Error: already inside a tmux session.\n"
            "Nesting is not recommended (prefix key conflicts,\n"
            "confusing detach behavior).\n"
            "Pass --allow-nesting to override.",
            err=True,
        )
        raise typer.Exit(1)

    ws = db.get_workspace(name)
    if ws is None:
        typer.echo(f"Error: workspace '{name}' not found", err=True)
        raise typer.Exit(1)

    if ws.type == "local":
        from agentworks.workspaces.backends.local import console_local_workspace

        db.update_workspace_last_seen(name)
        console_local_workspace(name, recreate=recreate)
    elif ws.type == "vm":
        vm = db.get_vm(ws.vm_name)  # type: ignore[arg-type]
        if vm is None:
            typer.echo(f"Error: VM '{ws.vm_name}' not found", err=True)
            raise typer.Exit(1)

        _guard_vm_status(vm)
        _ensure_vm_running(db, config, vm)
        db.update_workspace_last_seen(name)

        from agentworks.workspaces.backends.vm import console_vm_workspace

        console_vm_workspace(vm, config, name, recreate=recreate)
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

    def _tpl_name(t: str | None) -> str:
        if t is None or t == "(built-in)":
            return "default"
        return t

    rows = [
        (ws.name, ws.type, ws.vm_name or "-", _tpl_name(ws.template), ws.created_at)
        for ws in workspaces
    ]

    name_w = max(len("NAME"), max(len(r[0]) for r in rows))
    type_w = max(len("TYPE"), max(len(r[1]) for r in rows))
    vm_w = max(len("VM"), max(len(r[2]) for r in rows))
    tpl_w = max(len("TEMPLATE"), max(len(r[3]) for r in rows))

    header = f"{'NAME':<{name_w}}  {'TYPE':<{type_w}}  {'VM':<{vm_w}}  {'TEMPLATE':<{tpl_w}}  CREATED"
    typer.echo(header)
    typer.echo("-" * len(header))
    for ws_name, ws_type, vm_name, tpl, created in rows:
        typer.echo(f"{ws_name:<{name_w}}  {ws_type:<{type_w}}  {vm_name:<{vm_w}}  {tpl:<{tpl_w}}  {created}")


def delete_workspace(
    db: Database,
    config: Config,
    name: str,
    *,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Delete a workspace."""
    ws = db.get_workspace(name)
    if ws is None:
        typer.echo(f"Error: workspace '{name}' not found", err=True)
        raise typer.Exit(1)

    # Check for tasks
    task_count = len(db.list_tasks(workspace_name=name))
    if task_count > 0 and not force:
        typer.echo(
            f"Error: workspace '{name}' has {task_count} task(s). "
            "Delete them first, or use --force.",
            err=True,
        )
        raise typer.Exit(1)

    if not yes:
        msg = f"Delete workspace '{name}'?"
        if task_count > 0:
            msg += f" ({task_count} task(s) will also be deleted)"
        typer.confirm(msg, abort=True)

    # Kill running task sessions and delete task records
    if ws.type == "vm" and ws.vm_name:
        vm = db.get_vm(ws.vm_name)
        if vm is not None and vm.tailscale_host is not None:
            from functools import partial

            from agentworks.ssh import run, ssh_target_for_vm
            from agentworks.tasks.tmux import kill_task_session

            target = ssh_target_for_vm(vm, config)
            run_command = partial(run, target)
            for task in db.list_tasks(workspace_name=name):
                kill_task_session(name, task.name, run_command=run_command)
    db.delete_tasks_for_workspace(name)

    # Delete agents (remote cleanup + DB)
    from agentworks.agents.manager import delete_agents_for_workspace

    delete_agents_for_workspace(db, config, ws)

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


def _guard_vm_status(vm: VMRow) -> None:
    """Block operations on VMs that are not usable (failed or in-progress)."""
    usable = {InitStatus.COMPLETE.value, InitStatus.PARTIAL.value}
    if vm.init_status not in usable:
        if vm.init_status == InitStatus.FAILED.value:
            typer.echo(
                f"Error: VM '{vm.name}' is in 'failed' state. Run 'vm delete' and recreate.",
                err=True,
            )
        else:
            typer.echo(
                f"Error: VM '{vm.name}' initialization is not complete (status: {vm.init_status}).",
                err=True,
            )
        raise typer.Exit(1)


def _resolve_vm(db: Database, vm_name: str | None) -> VMRow:
    """Resolve which VM to use: explicit, auto-select if 1, or error."""
    if vm_name is not None:
        vm = db.get_vm(vm_name)
        if vm is None:
            typer.echo(f"Error: VM '{vm_name}' not found", err=True)
            raise typer.Exit(1)
        return vm

    vms = db.list_vms()
    usable_statuses = {InitStatus.COMPLETE.value, InitStatus.PARTIAL.value}
    usable_vms = [v for v in vms if v.init_status in usable_statuses]

    if len(usable_vms) == 0:
        typer.echo("Error: no VMs available. Create one with 'agentworks vm create'.", err=True)
        raise typer.Exit(1)

    if len(usable_vms) == 1:
        return usable_vms[0]

    typer.echo("Error: multiple VMs available. Specify --vm:", err=True)
    for v in usable_vms:
        typer.echo(f"  {v.name}", err=True)
    raise typer.Exit(1)


def _ensure_vm_running(db: Database, config: Config, vm: VMRow) -> None:
    """Auto-start a stopped/deallocated VM and verify Tailscale connectivity."""
    from agentworks.vms.manager import _ensure_tailscale, _get_provisioner_for_vm

    provisioner = _get_provisioner_for_vm(db, vm)
    status = provisioner.status(vm)

    if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        typer.echo(f"VM '{vm.name}' is {status.value}. Starting...")
        provisioner.start(vm)
        typer.echo(f"VM '{vm.name}' started")
        _ensure_tailscale(db, config, vm, provisioner)
