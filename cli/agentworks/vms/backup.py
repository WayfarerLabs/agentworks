"""VM backup -- export all metadata and workspace files to a local archive."""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import Database, WorkspaceRow
    from agentworks.ssh import ExecTarget, SSHTarget


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
    _vm, agents, workspaces, sessions, events, grants_by_agent = db.snapshot_vm_backup_data(vm_name)

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

        try:
            result = target.run(f"id -u {shlex.quote(agent.linux_user)}", check=False)
            if result.ok:
                agent_data["live_uid"] = result.stdout.strip()
            else:
                agent_data["live_uid"] = None
                typer.echo(f"    Warning: user '{agent.linux_user}' not found on VM", err=True)
        except SSHError:
            agent_data["live_uid"] = None

        agent_data["grants"] = [asdict(g) for g in grants_by_agent.get(agent.name, [])]
        agents_data.append(agent_data)
    _write_json(backup_dir / "agents.json", agents_data)

    # 4. Workspaces with live GID verification
    typer.echo(f"  Exporting {len(workspaces)} workspaces...")
    ws_data = []
    for ws in workspaces:
        ws_entry = asdict(ws)
        ws_group = f"ws--{ws.name}"

        try:
            result = target.run(f"getent group {shlex.quote(ws_group)}", check=False)
            if result.ok:
                parts = result.stdout.strip().split(":")
                ws_entry["live_gid"] = parts[2] if len(parts) > 2 else None
            else:
                ws_entry["live_gid"] = None
                typer.echo(f"    Warning: group '{ws_group}' not found on VM", err=True)
        except SSHError:
            ws_entry["live_gid"] = None

        ws_data.append(ws_entry)
    _write_json(backup_dir / "workspaces.json", ws_data)

    # 5. Sessions
    typer.echo(f"  Exporting {len(sessions)} sessions...")
    _write_json(backup_dir / "sessions.json", [asdict(s) for s in sessions])

    # 6. Workspace files -- single archive of all workspace paths
    vm_workspaces = [ws for ws in workspaces if ws.type == "vm"]

    if vm_workspaces:
        local_archive = backup_dir / "workspaces.tar.zst"
        try:
            _archive_workspaces(target, target_ssh, vm_workspaces, local_archive, timestamp)
        except Exception:
            db.insert_vm_event(vm_name, "backup_failed")
            raise
    else:
        typer.echo("  No VM workspaces to archive.")

    # 7. Manifest
    manifest = {
        "version": 2,
        "vm_name": vm_name,
        "timestamp": timestamp,
        "agent_count": len(agents_data),
        "workspace_count": len(ws_data),
        "session_count": len(sessions),
        "event_count": len(events),
        "workspace_paths": [ws.workspace_path for ws in vm_workspaces],
    }
    _write_json(backup_dir / "manifest.json", manifest)

    db.insert_vm_event(vm_name, "backup_completed", detail=str(backup_dir))
    ssh_logger.close()

    typer.echo(f"\nBackup complete: {backup_dir}")

    return backup_dir


def _archive_workspaces(
    target: ExecTarget,
    target_ssh: SSHTarget,
    vm_workspaces: list[WorkspaceRow],
    local_archive: Path,
    timestamp: str,
) -> None:
    """Create a single zstd-compressed tar of all workspace paths and transfer locally.

    Runs tar via nohup so it survives SSH disconnects. Polls for completion
    and reports archive size periodically.

    The archive is created in a root-owned temp directory to avoid symlink
    attacks and collisions in /tmp.
    """
    from agentworks.ssh import SSHError, copy_from

    # Create a secure temp directory (root-owned, mode 0700)
    tmp_dir = target.run_as_root("mktemp -d /tmp/agentworks-backup-XXXXXX").stdout.strip()
    q_tmp = shlex.quote(tmp_dir)
    archive = f"{tmp_dir}/workspaces.tar.zst"
    q_archive = shlex.quote(archive)
    log = f"{tmp_dir}/tar.log"
    q_log = shlex.quote(log)

    try:
        # Verify workspace paths exist on the VM
        valid = []
        for ws in vm_workspaces:
            if target.run(f"test -d {shlex.quote(ws.workspace_path)}", check=False).ok:
                valid.append(ws)
            else:
                typer.echo(f"    Warning: path not found, skipping: {ws.workspace_path}", err=True)

        if not valid:
            typer.echo("  Error: no workspace paths exist on the VM", err=True)
            raise typer.Exit(1)

        paths = " ".join(shlex.quote(ws.workspace_path.lstrip("/")) for ws in valid)

        # Verify zstd is available
        if not target.run("command -v zstd >/dev/null 2>&1", check=False).ok:
            typer.echo(
                "  Error: zstd is not installed on the VM.\n"
                "  Run 'agentworks vm reinit' to install it.",
                err=True,
            )
            raise typer.Exit(1)

        # Use zstd at level 15 for high compression (trades CPU for smaller archive,
        # which is worthwhile since cross-workspace deduplication benefits from it).
        typer.echo(f"  Archiving {len(valid)} workspace(s) with zstd (this may take a while)...")
        typer.echo(f"    Remote archive: {archive}")
        typer.echo(f"    Local archive:  {local_archive}")

        # Launch tar via nohup, capture PID via $!.
        # Everything runs inside sh -c so sudo covers the whole command
        # including the backgrounding and PID echo.
        pid_file = f"{tmp_dir}/tar.pid"
        q_pid = shlex.quote(pid_file)
        launch = target.run_as_root(
            f"sh -c 'ZSTD_CLEVEL=15 nohup tar --zstd -cf {q_archive} -C / -- {paths} "
            f">{q_log} 2>&1 </dev/null & echo $! > {q_pid}'",
            check=False,
        )
        if not launch.ok:
            typer.echo(f"  Error: failed to launch tar: {launch.stderr.strip()}", err=True)
            raise typer.Exit(1)

        # Read PID
        pid_result = target.run_as_root(f"cat {q_pid}", check=False)
        pid = pid_result.stdout.strip()
        if not pid.isdigit():
            typer.echo("  Error: tar process did not start", err=True)
            tar_log = target.run_as_root(f"cat {q_log}", check=False).stdout.strip()
            if tar_log:
                for line in tar_log.splitlines():
                    typer.echo(f"    {line}", err=True)
            raise typer.Exit(1)

        typer.echo(f"  tar started (PID {pid})")

        # Everything from here can be interrupted with Ctrl-C
        try:
            # Poll until process exits. Check every 15s, report size every 30s.
            last_report = time.monotonic()
            while target.run_as_root(f"kill -0 {pid} 2>/dev/null", check=False).ok:
                time.sleep(15)
                if time.monotonic() - last_report >= 30:
                    _report_size(target, archive)
                    last_report = time.monotonic()

            # Read tar log
            tar_log = target.run_as_root(f"cat {q_log}", check=False).stdout.strip()

            # Check if archive was created
            if not target.run_as_root(f"test -f {q_archive}", check=False).ok:
                typer.echo("  Error: tar failed", err=True)
                if tar_log:
                    for line in tar_log.splitlines():
                        typer.echo(f"    {line}", err=True)
                raise typer.Exit(1)

            _report_size(target, archive)

            if tar_log:
                typer.echo("  tar warnings:", err=True)
                for line in tar_log.splitlines()[-10:]:
                    typer.echo(f"    {line}", err=True)

            # Transfer to local. Chown the temp dir and archive to the admin
            # user so scp can read it (avoids making it world-readable).
            admin = shlex.quote(target_ssh.user or "agentworks")
            target.run_as_root(f"chown {admin} {q_tmp} {q_archive}")

            # Get remote archive size for progress reporting
            size_result = target.run_as_root(f"stat -c %s {q_archive}", check=False)
            remote_size = int(size_result.stdout.strip()) if size_result.ok else 0

            typer.echo("  Transferring remote archive to local...")
            _transfer_with_progress(target_ssh, archive, local_archive, remote_size)

        except KeyboardInterrupt:
            typer.echo("\n  Interrupted. Cleaning up...", err=True)
            target.run_as_root(f"kill {pid} 2>/dev/null", check=False)
            raise typer.Exit(1)

    finally:
        target.run_as_root(f"rm -rf {q_tmp}", check=False)


def _transfer_with_progress(
    target_ssh: SSHTarget,
    remote_path: str,
    local_path: Path,
    remote_size: int,
) -> None:
    """Transfer a file via scp with progress reporting based on local file size."""
    from agentworks.ssh import SSHError

    # Build scp args (same as copy_from but we manage the subprocess ourselves)
    args = ["scp", "-q", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    if target_ssh.port is not None:
        args.extend(["-P", str(target_ssh.port)])
    if target_ssh.identity_file is not None:
        args.extend(["-i", str(target_ssh.identity_file)])
    src = f"{target_ssh.user}@{target_ssh.host}:{remote_path}" if target_ssh.user else f"{target_ssh.host}:{remote_path}"
    args.append(src)
    args.append(str(local_path))

    # Run scp in a thread so we can poll local file size
    result_holder: list[subprocess.CompletedProcess[str]] = []
    error_holder: list[Exception] = []

    def _run_scp() -> None:
        try:
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
            result_holder.append(r)
        except Exception as e:
            error_holder.append(e)

    thread = threading.Thread(target=_run_scp, daemon=True)
    thread.start()

    # Poll local file size every 15s, report every 30s
    last_report = time.monotonic()
    while thread.is_alive():
        thread.join(timeout=15)
        if not thread.is_alive():
            break
        if time.monotonic() - last_report >= 30:
            try:
                local_size = local_path.stat().st_size
                if remote_size > 0:
                    pct = local_size / remote_size * 100
                    typer.echo(f"  Transfer: {_fmt_size(local_size)} / {_fmt_size(remote_size)} ({pct:.0f}%)")
                else:
                    typer.echo(f"  Transfer: {_fmt_size(local_size)}")
            except FileNotFoundError:
                pass
            last_report = time.monotonic()

    if error_holder:
        raise error_holder[0]

    if not result_holder:
        raise SSHError("scp did not produce a result")

    result = result_holder[0]
    if result.returncode != 0:
        raise SSHError(f"scp failed: {result.stderr.strip()}")

    typer.echo(f"  Saved: {local_path} ({_fmt_size(local_path.stat().st_size)})")


def _fmt_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string."""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024**3):.1f} GB"
    return f"{size_bytes / (1024**2):.1f} MB"


def _report_size(target: ExecTarget, remote_path: str) -> None:
    """Print the size of a remote file."""
    try:
        result = target.run_as_root(f"stat -c %s {shlex.quote(remote_path)}", check=False)
        if result.ok:
            typer.echo(f"  Archive size: {_fmt_size(int(result.stdout.strip()))}")
    except Exception:
        pass


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")
