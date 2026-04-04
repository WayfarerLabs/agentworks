"""VM backup -- export all metadata and workspace files to a local archive."""

from __future__ import annotations

import json
import shlex
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import Database


def backup_vm(
    db: Database,
    config: Config,
    vm_name: str,
) -> Path:
    """Create a full backup of a VM: metadata + workspace files.

    Returns the path to the backup archive.
    """
    from agentworks.ssh import SSHError, SSHLogger, ssh_target_for_vm
    from agentworks.workspaces.manager import _ensure_vm_running

    vm = db.get_vm(vm_name)
    if vm is None:
        typer.echo(f"Error: VM '{vm_name}' not found", err=True)
        raise typer.Exit(1)
    _ensure_vm_running(db, config, vm)

    if vm.tailscale_host is None:
        typer.echo(f"Error: VM '{vm_name}' has no Tailscale address", err=True)
        raise typer.Exit(1)

    # Create backup directory first so the log goes inside it
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"{vm_name}-{timestamp}"
    backup_dir = config.paths.backups / backup_name
    backup_dir.mkdir(parents=True, exist_ok=True)

    ssh_logger = SSHLogger(vm_name, "vm-backup")
    ssh_logger.path = backup_dir / "backup.log"
    target_ssh = ssh_target_for_vm(vm, config)

    from agentworks.ssh import ExecTarget

    target = ExecTarget(ssh=target_ssh, logger=ssh_logger)

    # Log the backup event
    db.insert_vm_event(vm_name, "backup_started")

    typer.echo(f"Backing up VM '{vm_name}' to {backup_dir}...")

    # Snapshot all DB data in a single transaction for consistency
    typer.echo("  Reading database (consistent snapshot)...")
    _vm, agents, workspaces, tasks, events, grants_by_agent = db.snapshot_vm_backup_data(vm_name)

    # 1. VM metadata
    typer.echo("  Exporting VM metadata...")
    _write_json(backup_dir / "vm.json", asdict(vm))

    # 2. Events
    typer.echo(f"  Exporting {len(events)} VM events...")
    _write_json(backup_dir / "events.json", [asdict(e) for e in events])

    # 3. Agents with grants and live UID verification
    typer.echo(f"  Exporting {len(agents)} agents...")
    agents_data = []
    for agent in agents:
        agent_data = asdict(agent)

        # Verify UID on VM
        try:
            result = target.run(f"id -u {shlex.quote(agent.linux_user)}", check=False)
            if result.ok:
                live_uid = result.stdout.strip()
                agent_data["live_uid"] = live_uid
            else:
                agent_data["live_uid"] = None
                typer.echo(f"    Warning: user '{agent.linux_user}' not found on VM", err=True)
        except SSHError:
            agent_data["live_uid"] = None

        # Grants (from snapshot)
        agent_data["grants"] = [asdict(g) for g in grants_by_agent.get(agent.name, [])]
        agents_data.append(agent_data)
    _write_json(backup_dir / "agents.json", agents_data)

    # 4. Workspaces with live GID verification
    typer.echo(f"  Exporting {len(workspaces)} workspaces...")
    ws_data = []
    for ws in workspaces:
        ws_entry = asdict(ws)
        ws_group = f"ws--{ws.name}"

        # Verify GID on VM
        try:
            result = target.run(f"getent group {shlex.quote(ws_group)}", check=False)
            if result.ok:
                # getent group output: name:x:gid:members
                parts = result.stdout.strip().split(":")
                ws_entry["live_gid"] = parts[2] if len(parts) > 2 else None
            else:
                ws_entry["live_gid"] = None
                typer.echo(f"    Warning: group '{ws_group}' not found on VM", err=True)
        except SSHError:
            ws_entry["live_gid"] = None

        ws_data.append(ws_entry)
    _write_json(backup_dir / "workspaces.json", ws_data)

    # 5. Tasks
    typer.echo(f"  Exporting {len(tasks)} tasks...")
    _write_json(backup_dir / "tasks.json", [asdict(t) for t in tasks])

    # 6. Workspace files -- use a single remote temp dir for all archives
    vm_workspaces = [ws for ws in workspaces if ws.type == "vm"]
    ws_files_dir = backup_dir / "workspaces"
    ws_files_dir.mkdir(exist_ok=True)

    if vm_workspaces:
        # Create a temp dir on the VM
        remote_tmp_dir = target.run("mktemp -d").stdout.strip()
        try:
            for ws in vm_workspaces:
                typer.echo(f"  Archiving workspace '{ws.name}'...")
                try:
                    archive_name = f"{ws.name}.tar.gz"
                    remote_archive = f"{remote_tmp_dir}/{archive_name}"
                    target.run_as_root(
                        f"tar czf {shlex.quote(remote_archive)} -C {shlex.quote(ws.workspace_path)} .",
                        timeout=300,
                    )
                    target.run_as_root(f"chmod a+r {shlex.quote(remote_archive)}")
                    target.copy_from(remote_archive, str(ws_files_dir / archive_name), timeout=300)
                    typer.echo(f"    {archive_name}")
                except SSHError as e:
                    typer.echo(f"    Warning: failed to archive '{ws.name}': {e}", err=True)
        finally:
            target.run_as_root(f"rm -rf {shlex.quote(remote_tmp_dir)}", check=False)

    # 7. Manifest
    manifest = {
        "version": 1,
        "vm_name": vm_name,
        "timestamp": timestamp,
        "agent_count": len(agents_data),
        "workspace_count": len(ws_data),
        "task_count": len(tasks),
        "event_count": len(events),
    }
    _write_json(backup_dir / "manifest.json", manifest)

    db.insert_vm_event(vm_name, "backup_completed", detail=str(backup_dir))
    ssh_logger.close()

    typer.echo(f"\nBackup complete: {backup_dir}")

    return backup_dir


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")
