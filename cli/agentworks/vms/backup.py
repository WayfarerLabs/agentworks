"""VM backup: export all metadata and workspace files to a local archive."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import BackupError, NotFoundError, StateError
from agentworks.vms.manager import gated_vm_boundary

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import Database, WorkspaceRow
    from agentworks.transports import SSHTransport, Transport


def backup_vm(
    db: Database,
    config: Config,
    vm_name: str,
) -> Path:
    """Create a full backup of a VM: metadata + workspace files.

    Returns the path to the backup archive.

    Orchestrated (``vms.manager.gated_vm_boundary``): the graph derives
    from the VM's row, the activation gate replaces this command's
    ``keep_active`` use (opening BEFORE the preflight sweep; its
    just-in-time values seed the boundary resolver), and the
    held-active span covers the whole snapshot-archive-transfer body.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.ssh import SSHError, SSHLogger
    from agentworks.transports import SSHTransport, transport

    vm = db.get_vm(vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{vm_name}' not found",
            entity_kind="vm",
            entity_name=vm_name,
        )
    # Deterministic fatal checks BEFORE the boundary: the composition
    # root runs the preflight sweep and the boundary resolve pass,
    # which can prompt for site secrets; the operator must never
    # answer a prompt for a backup this row already sank.
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{vm_name}' has no Tailscale address",
            entity_kind="vm",
            entity_name=vm_name,
        )
    registry = build_registry(config)

    with gated_vm_boundary(db, config, registry, vm):
        # Create backup directory first so the log goes inside it
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_name = f"{vm_name}-{timestamp}"
        backup_dir = config.paths.backups / backup_name
        backup_dir.mkdir(parents=True, exist_ok=True)

        ssh_logger = SSHLogger(vm_name, "vm-backup")
        ssh_logger.path = backup_dir / "backup.log"
        target = transport(vm, config, logger=ssh_logger)
        # backup_vm runs over the admin Tailscale SSH transport, so this is
        # always an SSHTransport. _archive_workspaces and _transfer_with_progress
        # rely on SSH-specific scp argv building for progress reporting.
        assert isinstance(target, SSHTransport)

        # Log the backup event
        db.insert_vm_event(vm_name, "backup_started")

        output.info(f"Backing up VM '{vm_name}' to {backup_dir}...")

        # Single try/except/finally around the whole backup body so any failure
        # (SSH timeout in the agents/workspaces loops, an archive failure, a
        # disk-full _write_json, etc.) consistently emits backup_failed AND
        # closes the SSH logger so the log keeps its footer. Previously only
        # _archive_workspaces had a try/except, so other failure paths left
        # the DB without an event and the log without its trailing summary.
        try:
            # Snapshot all DB data in a single transaction for consistency
            output.detail("Reading database (consistent snapshot)...")
            _vm, agents, workspaces, sessions, events, grants_by_agent = db.snapshot_vm_backup_data(vm_name)

            # 1. VM metadata
            output.detail("Exporting VM metadata...")
            _write_json(backup_dir / "vm.json", asdict(vm))

            # 2. Events
            output.detail(f"Exporting {len(events)} VM events...")
            _write_json(backup_dir / "events.json", [asdict(e) for e in events])

            # 3. Agents with grants and live UID verification
            output.detail(f"Exporting {len(agents)} agents...")
            agents_data = []
            for agent in agents:
                agent_data = asdict(agent)

                try:
                    result = target.run(f"id -u {shlex.quote(agent.linux_user)}", check=False)
                    if result.ok:
                        agent_data["live_uid"] = result.stdout.strip()
                    else:
                        agent_data["live_uid"] = None
                        output.warn(f"user '{agent.linux_user}' not found on VM")
                except SSHError:
                    agent_data["live_uid"] = None

                agent_data["grants"] = [asdict(g) for g in grants_by_agent.get(agent.name, [])]
                agents_data.append(agent_data)
            _write_json(backup_dir / "agents.json", agents_data)

            # 4. Workspaces with live GID verification
            output.detail(f"Exporting {len(workspaces)} workspaces...")
            ws_data = []
            for ws in workspaces:
                ws_entry = asdict(ws)
                ws_group = ws.linux_group
                try:
                    result = target.run(f"getent group {shlex.quote(ws_group)}", check=False)
                    if result.ok:
                        parts = result.stdout.strip().split(":")
                        ws_entry["live_gid"] = parts[2] if len(parts) > 2 else None
                    else:
                        ws_entry["live_gid"] = None
                        output.warn(f"group '{ws_group}' not found on VM")
                except SSHError:
                    ws_entry["live_gid"] = None

                ws_data.append(ws_entry)
            _write_json(backup_dir / "workspaces.json", ws_data)

            # 5. Sessions
            output.detail(f"Exporting {len(sessions)} sessions...")
            _write_json(backup_dir / "sessions.json", [asdict(s) for s in sessions])

            # 6. Workspace files -- single archive of all workspace paths.
            # _archive_workspaces catches its own KeyboardInterrupt during the
            # long tar phase and converts it to UserAbort (an AgentworksError),
            # which the outer except below treats as a backup_failed event.
            archived_paths: list[str] = []
            skipped_paths: list[str] = []
            if workspaces:
                local_archive = backup_dir / "workspaces.tar.zst"
                archived_paths, skipped_paths = _archive_workspaces(
                    target, workspaces, local_archive,
                )
            else:
                output.detail("No VM workspaces to archive.")

            # 7. Manifest
            manifest = {
                "version": 2,
                "vm_name": vm_name,
                "timestamp": timestamp,
                "agent_count": len(agents_data),
                "workspace_count": len(ws_data),
                "session_count": len(sessions),
                "event_count": len(events),
                "archived_paths": archived_paths,
                "skipped_paths": skipped_paths,
            }
            _write_json(backup_dir / "manifest.json", manifest)

            db.insert_vm_event(vm_name, "backup_completed", detail=str(backup_dir))
            output.info(f"\nBackup complete: {backup_dir}")
            return backup_dir
        except Exception:
            db.insert_vm_event(vm_name, "backup_failed")
            raise
        finally:
            ssh_logger.close()


def _archive_workspaces(
    target: SSHTransport,
    vm_workspaces: list[WorkspaceRow],
    local_archive: Path,
) -> tuple[list[str], list[str]]:
    """Create a single zstd-compressed tar of all workspace paths and transfer locally.

    Runs tar via nohup so it survives SSH disconnects. Polls for completion
    and reports archive size periodically.

    The archive is created in a root-owned temp directory to avoid symlink
    attacks and collisions in /tmp.

    Returns (archived_paths, skipped_paths) -- paths that were actually included
    and paths that were skipped because they didn't exist on the VM.
    """

    # Create a secure temp directory (root-owned, mode 0700)
    tmp_dir = target.run("mktemp -d /tmp/agentworks-backup-XXXXXX", sudo=True).stdout.strip()
    q_tmp = shlex.quote(tmp_dir)
    archive = f"{tmp_dir}/workspaces.tar.zst"
    q_archive = shlex.quote(archive)

    try:
        # Verify workspace paths exist on the VM
        valid: list[WorkspaceRow] = []
        skipped: list[str] = []
        for ws in vm_workspaces:
            if target.run(f"test -d {shlex.quote(ws.workspace_path)}", sudo=True, check=False).ok:
                valid.append(ws)
            else:
                output.warn(f"path not found, skipping: {ws.workspace_path}")
                skipped.append(ws.workspace_path)

        if not valid:
            raise BackupError("no workspace paths exist on the VM")

        # Verify zstd is available
        if not target.run("command -v zstd >/dev/null 2>&1", check=False).ok:
            raise BackupError(
                "zstd is not installed on the VM.",
                hint="Run 'agw vm reinit' to install it.",
            )

        # Calculate total uncompressed size
        du_paths = " ".join(shlex.quote(ws.workspace_path) for ws in valid)
        du_result = target.run(f"du -sb {du_paths} | awk '{{s+=$1}} END {{print s}}'", sudo=True, check=False)
        if du_result.ok and du_result.stdout.strip().isdigit():
            total_size = int(du_result.stdout.strip())
            output.detail(f"Total workspace size: {_fmt_size(total_size)} (uncompressed)")

        # Use zstd at level 15 for high compression (trades CPU for smaller archive,
        # which is worthwhile since cross-workspace deduplication benefits from it).
        output.detail(f"Archiving {len(valid)} workspace(s) with zstd (this may take a while)...")
        # The archive paths sit one level under the "Archiving ..." line
        # (was detail(indent=2)).
        with output.section():
            output.detail(f"Remote archive: {archive}")
            output.detail(f"Local archive:  {local_archive}")

        # Write paths file via scp to avoid shell escaping issues.
        paths_file = f"{tmp_dir}/paths.txt"
        q_paths_file = shlex.quote(paths_file)
        path_content = "\n".join(ws.workspace_path.lstrip("/") for ws in valid) + "\n"

        # Admin can't write to root-owned temp dir, so stage via a securely
        # created temp file (mktemp creates with mode 0600), then move as root.
        staging_paths = target.run("mktemp /tmp/_aw_paths_XXXXXX.txt").stdout.strip()
        q_staging = shlex.quote(staging_paths)
        target.write_file(staging_paths, path_content)
        target.run(f"mv {q_staging} {q_paths_file}", sudo=True)

        # Use run_detached in a background thread so we can poll archive size.
        # run_detached handles nohup reliably via scp'd wrapper script.
        tar_cmd = f"ZSTD_CLEVEL=15 tar --zstd -cf {q_archive} -C / -T {q_paths_file}"

        # Create a secure admin-owned directory (mktemp -d creates mode 0700)
        # for run_detached's files. Can't use the root-owned tmp_dir because
        # run_detached writes its wrapper script via scp (as admin). Using
        # mktemp -d (not -u) avoids the race/symlink risks of mktemp -u.
        detached_dir = target.run("mktemp -d /tmp/_aw_detached_XXXXXX").stdout.strip()
        detached_base = f"{detached_dir}/run"

        import threading

        from agentworks.remote_exec import DetachedResult, run_detached

        result_holder: list[DetachedResult] = []
        error_holder: list[Exception] = []

        def _run_tar() -> None:
            try:
                r = run_detached(
                    target,
                    tar_cmd,
                    label="Archive",
                    base_path=detached_base,
                    poll_interval=5,
                    quiet_timeout=300,
                    as_root=True,
                    quiet=True,
                )
                result_holder.append(r)
            except Exception as e:
                error_holder.append(e)

        thread = threading.Thread(target=_run_tar, daemon=True)
        thread.start()

        # Poll archive size while tar runs
        try:
            last_report = time.monotonic()
            while thread.is_alive():
                thread.join(timeout=15)
                if thread.is_alive() and time.monotonic() - last_report >= 30:
                    _report_size(target, archive)
                    last_report = time.monotonic()
        except KeyboardInterrupt:
            output.warn("Interrupted. Killing remote tar and cleaning up...")
            # Read the PID that run_detached's wrapper wrote, kill the process group
            pid_result = target.run(f"cat {shlex.quote(detached_base)}.pid", sudo=True, check=False)
            pid = pid_result.stdout.strip() if pid_result.ok else ""
            if pid.isdigit():
                # Kill the wrapper shell's process group (tar + wrapper)
                target.run(f"kill -TERM -{pid} 2>/dev/null", sudo=True, check=False)
            from agentworks.errors import UserAbort

            raise UserAbort("backup interrupted") from None

        if error_holder:
            raise error_holder[0]
        if not result_holder:
            raise BackupError("tar did not produce a result")

        result = result_holder[0]
        if result.exit_code != 0:
            detail = f"Command: {tar_cmd}"
            if result.output:
                detail += f"\nOutput:\n{result.output.strip()}"
            raise BackupError(f"tar failed (exit {result.exit_code})\n{detail}")

        _report_size(target, archive)

        if result.output.strip():
            output.warn("tar warnings:")
            # The captured tar-warning lines sit one level under the
            # "tar warnings:" line (was detail(indent=2)).
            with output.section():
                for line in result.output.strip().splitlines()[-10:]:
                    output.detail(line)

        # Transfer to local. Chown the temp dir and archive to the admin
        # user so scp can read it (avoids making it world-readable).
        admin = shlex.quote(target.user or "agentworks")
        target.run(f"chown {admin} {q_tmp} {q_archive}", sudo=True)

        # Get remote archive size for progress reporting
        size_result = target.run(f"stat -c %s {q_archive}", sudo=True, check=False)
        remote_size = int(size_result.stdout.strip()) if size_result.ok else 0

        output.detail("Transferring remote archive to local...")
        _transfer_with_progress(target, archive, local_archive, remote_size)

    except Exception:
        output.warn(f"Remote temp dir preserved for debugging: {tmp_dir}")
        raise
    else:
        target.run(f"rm -rf {q_tmp}", sudo=True, check=False)
        target.run(f"rm -rf {shlex.quote(detached_dir)}", check=False)

    return [ws.workspace_path for ws in valid], skipped


def _transfer_with_progress(
    target: SSHTransport,
    remote_path: str,
    local_path: Path,
    remote_size: int,
) -> None:
    """Transfer a file via scp with progress reporting based on local file size.

    Uses Popen so the process can be terminated on Ctrl-C and the partially
    downloaded file cleaned up. SSH-only because we bypass the polymorphic
    ``copy_from`` to drive scp via ``Popen`` for the progress reporting.
    """
    from agentworks.ssh import SSHError

    args = ["scp", "-q", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    if target.port is not None:
        args.extend(["-P", str(target.port)])
    if target.identity_file is not None:
        args.extend(["-i", str(target.identity_file)])
    if target.proxy_jump is not None:
        args.extend(["-J", target.proxy_jump])
    src = f"{target.user}@{target.host}:{remote_path}" if target.user else f"{target.host}:{remote_path}"
    args.append(src)
    args.append(str(local_path))

    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        last_report = time.monotonic()
        while proc.poll() is None:
            time.sleep(15)
            if time.monotonic() - last_report >= 30:
                try:
                    local_size = local_path.stat().st_size
                    if remote_size > 0:
                        pct = local_size / remote_size * 100
                        output.detail(
                            f"Transfer: {_fmt_size(local_size)} / "
                            f"{_fmt_size(remote_size)} ({pct:.0f}%)"
                        )
                    else:
                        output.detail(f"Transfer: {_fmt_size(local_size)}")
                except FileNotFoundError:
                    pass
                last_report = time.monotonic()

        if proc.returncode != 0:
            assert proc.stderr is not None
            stderr = (proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
            raise SSHError(f"scp failed: {stderr}")

        output.detail(f"Saved: {local_path} ({_fmt_size(local_path.stat().st_size)})")

    except (KeyboardInterrupt, Exception):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # Clean up partial download
        if local_path.exists():
            local_path.unlink()
        raise


def _fmt_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string."""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024**3):.1f} GB"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024**2):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _report_size(target: Transport, remote_path: str) -> None:
    """Print the size of a remote file."""
    try:
        result = target.run(f"stat -c %s {shlex.quote(remote_path)}", sudo=True, check=False)
        if result.ok:
            output.detail(f"Archive size: {_fmt_size(int(result.stdout.strip()))}")
    except Exception:
        pass


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")
