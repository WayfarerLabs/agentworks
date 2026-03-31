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

    from agentworks.ssh import SSHLogger

    ssh_logger = SSHLogger(vm.name, "workspace-create")

    try:
        typer.echo(f"Creating workspace '{ws_name}' on VM '{vm.name}' (template: {template_name})...")
        workspace_path = create_vm_workspace(vm, config, ws_name, template, logger=ssh_logger)

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
        ssh_logger.close()
        _cleanup()
        raise
    except Exception as e:
        ssh_logger.close()
        typer.echo(f"Error creating workspace: {e}", err=True)
        typer.echo(f"  SSH log: {ssh_logger.path}", err=True)
        _cleanup()
        raise typer.Exit(1) from None

    # Add grant_all agents to the new workspace group
    grant_all_agents = db.list_agents_on_vm_with_grant_all(vm.name)
    if grant_all_agents:
        from agentworks.agents.manager import _add_to_workspace_group

        for agent in grant_all_agents:
            _add_to_workspace_group(vm, config, agent.linux_user, ws_name, logger=ssh_logger)
            db.insert_agent_grant(agent.name, ws_name, "explicit")
        typer.echo(f"  Added {len(grant_all_agents)} grant-all agent(s) to workspace")

    ssh_logger.close()

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

    # Create SSH logger for VM operations
    ssh_logger = None
    if ws.type == "vm" and ws.vm_name:
        from agentworks.ssh import SSHLogger

        ssh_logger = SSHLogger(ws.vm_name, "workspace-delete")

    # Kill running task sessions and delete task records
    if ws.type == "vm" and ws.vm_name:
        vm = db.get_vm(ws.vm_name)
        if vm is not None and vm.tailscale_host is not None:
            from functools import partial

            from agentworks.ssh import run, ssh_target_for_vm
            from agentworks.tasks.tmux import kill_task_session

            target = ssh_target_for_vm(vm, config)
            run_command = partial(run, target, logger=ssh_logger)
            for task in db.list_tasks(workspace_name=name):
                kill_task_session(name, task.name, run_command=run_command)
    db.delete_tasks_for_workspace(name)

    # Revoke agent workspace grants (agents are VM-scoped, not deleted with workspaces)
    if ws.type == "vm" and ws.vm_name:
        vm_for_grants = db.get_vm(ws.vm_name)
        if vm_for_grants:
            from agentworks.agents.manager import revoke_workspace_grants

            revoke_workspace_grants(db, config, name, vm_for_grants)

    if ws.type == "local":
        from agentworks.workspaces.backends.local import delete_local_workspace

        delete_local_workspace(name, ws.workspace_path)
    elif ws.type == "vm" and ws.vm_name:
        vm = db.get_vm(ws.vm_name)
        if vm is not None:
            from agentworks.workspaces.backends.vm import delete_vm_workspace

            delete_vm_workspace(vm, config, name, ws.workspace_path, logger=ssh_logger)

    if ssh_logger is not None:
        ssh_logger.close()

    # Remove .code-workspace file
    code_ws_path = config.paths.code_workspaces / f"{name}.code-workspace"
    code_ws_path.unlink(missing_ok=True)

    db.delete_workspace(name)
    typer.echo(f"Workspace '{name}' deleted")


def copy_workspace(
    db: Database,
    config: Config,
    source_name: str,
    *,
    dest_name: str,
    vm_name: str | None = None,
    local: bool = False,
) -> None:
    """Copy a workspace to a new location."""
    import tempfile
    from pathlib import Path

    from agentworks.ssh import ssh_target_for_vm

    validate_name(dest_name)

    src_ws = db.get_workspace(source_name)
    if src_ws is None:
        typer.echo(f"Error: workspace '{source_name}' not found", err=True)
        raise typer.Exit(1)

    if db.get_workspace(dest_name) is not None:
        typer.echo(f"Error: workspace '{dest_name}' already exists", err=True)
        raise typer.Exit(1)

    # Create a temp file for the archive
    with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        # --- Pack from source ---
        if src_ws.type == "local":
            typer.echo(f"Packing workspace '{source_name}'...")
            result = subprocess.run(
                ["tar", "czf", str(tmp_path), "-C", src_ws.workspace_path, "."],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                typer.echo(f"Error: tar failed: {result.stderr.strip()}", err=True)
                raise typer.Exit(1)
        elif src_ws.type == "vm":
            src_vm = db.get_vm(src_ws.vm_name)  # type: ignore[arg-type]
            if src_vm is None:
                typer.echo(f"Error: VM '{src_ws.vm_name}' not found", err=True)
                raise typer.Exit(1)
            _guard_vm_status(src_vm)
            _ensure_vm_running(db, config, src_vm)
            if src_vm.tailscale_host is None:
                typer.echo(f"Error: VM '{src_vm.name}' has no Tailscale address", err=True)
                raise typer.Exit(1)

            src_target = ssh_target_for_vm(src_vm, config)
            typer.echo(f"Packing workspace '{source_name}' from VM '{src_vm.name}'...")

            # Stream tar from VM to local temp file
            ssh_args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
            if src_target.identity_file is not None:
                ssh_args.extend(["-i", str(src_target.identity_file)])
            ssh_args.append(f"{src_target.user}@{src_target.host}")
            ssh_args.append(f"tar czf - -C {src_ws.workspace_path} .")

            with open(tmp_path, "wb") as f:
                proc = subprocess.run(ssh_args, stdout=f, stderr=subprocess.PIPE)
            if proc.returncode != 0:
                stderr = proc.stderr.decode() if proc.stderr else ""
                typer.echo(f"Error: pack failed: {stderr.strip()}", err=True)
                raise typer.Exit(1)
        else:
            typer.echo(f"Error: unknown workspace type '{src_ws.type}'", err=True)
            raise typer.Exit(1)

        # --- Unpack to destination ---
        if local:
            workspace_path = str(config.paths.local_workspaces / dest_name)
            Path(workspace_path).mkdir(parents=True, exist_ok=True)

            typer.echo(f"Unpacking to local workspace '{dest_name}'...")
            result = subprocess.run(
                ["tar", "xzf", str(tmp_path), "-C", workspace_path],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                typer.echo(f"Error: tar failed: {result.stderr.strip()}", err=True)
                raise typer.Exit(1)

            db.insert_workspace(
                dest_name, ws_type="local", workspace_path=workspace_path, template="copied",
            )
        else:
            from agentworks.ssh import SSHLogger, copy_to, run

            dest_vm = _resolve_vm(db, vm_name)
            _guard_vm_status(dest_vm)
            _ensure_vm_running(db, config, dest_vm)
            if dest_vm.tailscale_host is None:
                typer.echo(f"Error: VM '{dest_vm.name}' has no Tailscale address", err=True)
                raise typer.Exit(1)

            lg = SSHLogger(dest_vm.name, "workspace-copy")
            dest_target = ssh_target_for_vm(dest_vm, config)
            workspace_path = f"/home/{dest_vm.admin_username}/workspaces/{dest_name}"

            typer.echo(f"Unpacking to workspace '{dest_name}' on VM '{dest_vm.name}'...")
            run(dest_target, f"mkdir -p {workspace_path}", timeout=10, logger=lg)

            remote_tmp = f"/tmp/{dest_name}-copy.tgz"
            copy_to(dest_target, tmp_path, remote_tmp, timeout=300)
            run(dest_target, f"tar xzf {remote_tmp} -C {workspace_path}", timeout=120, logger=lg)
            run(dest_target, f"rm -f {remote_tmp}", check=False, timeout=10, logger=lg)

            db.insert_workspace(
                dest_name, ws_type="vm", vm_name=dest_vm.name,
                workspace_path=workspace_path, template="copied",
            )

            # Generate tmuxinator config and VS Code workspace
            from agentworks.ssh import write_file
            from agentworks.workspaces.backends.vm import generate_code_workspace
            from agentworks.workspaces.tmuxinator import console_session_name, generate_config

            tmux_config = generate_config(dest_name, workspace_path)
            write_file(dest_target, f"{workspace_path}/.tmuxinator.yml", tmux_config, logger=lg)
            session = console_session_name(dest_name)
            run(dest_target, "mkdir -p ~/.config/tmuxinator", timeout=10, logger=lg)
            run(
                dest_target,
                f"ln -sf {workspace_path}/.tmuxinator.yml ~/.config/tmuxinator/{session}.yml",
                timeout=10,
                logger=lg,
            )
            code_ws_path = generate_code_workspace(dest_vm, config, dest_name, workspace_path)
            typer.echo(f"  VS Code workspace: {code_ws_path}")
            lg.close()
    finally:
        tmp_path.unlink(missing_ok=True)

    typer.echo(f"Workspace '{source_name}' copied to '{dest_name}'")


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
        typer.echo(f"Using VM '{usable_vms[0].name}'")
        return usable_vms[0]

    typer.echo("Select a VM:")
    for i, v in enumerate(usable_vms, 1):
        typer.echo(f"  {i}) {v.name}  ({v.platform})")

    choice = int(typer.prompt("VM number", type=int))
    if choice < 1 or choice > len(usable_vms):
        typer.echo(f"Error: invalid choice {choice}", err=True)
        raise typer.Exit(1)

    return usable_vms[choice - 1]


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
