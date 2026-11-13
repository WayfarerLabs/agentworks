"""VM backup: export all metadata and workspace files to a local archive."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

import agentworks.execution as execution
from agentworks import output
from agentworks.errors import AgentworksError, BackupError, NotFoundError, StateError, UserAbort
from agentworks.execution import detached_program, reachability
from agentworks.execution.access import Admin
from agentworks.execution.detached import State
from agentworks.execution.errors import ExecutionIncomplete
from agentworks.execution.requests import Command, Script, Shell, ToFile
from agentworks.execution.results import Completed
from agentworks.path_rendering import format_host_path
from agentworks.terminal import sanitize_terminal_output
from agentworks.vms.applied_state import canonicalize_vm_applied_slice
from agentworks.vms.manager import _guard_incomplete_vm, gated_vm_boundary, operation_log

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import Database, WorkspaceRow
    from agentworks.execution.access import VMAccess
    from agentworks.execution.detached import Detached, Launch, Observation
    from agentworks.secrets.policy import TtyInteractionPolicy


def backup_vm(
    db: Database,
    config: Config,
    vm_name: str,
    *,
    interaction: TtyInteractionPolicy,
) -> Path:
    """Create a full backup of a VM: metadata + workspace files.

    Returns the path to the backup archive.

    Orchestrated (``vms.manager.gated_vm_boundary``): the graph derives
    from the VM's row, the activation gate replaces this command's
    ``keep_active`` use (opening BEFORE the preflight sweep; its
    just-in-time values seed the boundary resolver), and the
    held-active span covers the whole snapshot-archive-transfer body.
    """
    from agentworks.bootstrap import load_request_registry

    vm = db.get_vm(vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{vm_name}' not found",
            entity_kind="vm",
            entity_name=vm_name,
        )
    # An unattested VM is refused like every ordinary command; a failed
    # initialization only warns, since a backup is how its data is kept.
    _guard_incomplete_vm(vm, allow_failed_init=True)
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
    if not _host_supports_private_backup_permissions() and db.instance_state.has_vm_owner_tree_desired_overlay(vm_name):
        _raise_windows_overlay_backup_error(vm_name)
    execution.resolve_ssh()
    registry = load_request_registry(config, live_database=db)

    with gated_vm_boundary(db, config, registry, vm, interaction=interaction):
        # Create backup directory first so the log goes inside it
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_name = f"{vm_name}-{timestamp}"
        backup_dir = config.paths.backups / backup_name
        _create_backup_directory(backup_dir, vm_name)

        # The log closes, with its footer, however the body ends, and any failure records backup_failed.
        with operation_log(vm_name, "vm-backup", path=backup_dir / "backup.log") as log:
            access = execution.vm_access(vm, config, Admin(), log=log.record)
            db.insert_vm_event(vm_name, "backup_started")
            try:
                return _back_up(db, access, vm_name, timestamp, backup_dir)
            except KeyboardInterrupt:
                db.insert_vm_event(vm_name, "backup_failed")
                raise UserAbort("backup interrupted") from None
            except Exception:
                db.insert_vm_event(vm_name, "backup_failed")
                raise


def _back_up(db: Database, access: VMAccess, vm_name: str, timestamp: str, backup_dir: Path) -> Path:
    """Export the VM's metadata and archive its workspaces into ``backup_dir``; return it."""
    with output.section(f"Backing up VM '{vm_name}' to {format_host_path(backup_dir)}"):
        # Snapshot all DB data in a single transaction for consistency
        output.info("Reading database (consistent snapshot)...")
        (
            snapshot_vm,
            agents,
            workspaces,
            sessions,
            events,
            grants_by_agent,
            desired_overlays,
            applied_slices,
        ) = db.snapshot_vm_backup_data(vm_name)
        if snapshot_vm is None:
            raise StateError(
                f"VM '{vm_name}' no longer exists",
                entity_kind="vm",
                entity_name=vm_name,
            )
        if not _host_supports_private_backup_permissions() and desired_overlays:
            _raise_windows_overlay_backup_error(vm_name)

        # 1. VM metadata
        output.info("Exporting VM metadata...")
        _write_json(backup_dir / "vm.json", asdict(snapshot_vm))

        # 2. Events
        output.info(f"Exporting {len(events)} VM events...")
        _write_json(backup_dir / "events.json", [asdict(e) for e in events])

        # 3. Agents with grants and live UID verification
        output.info(f"Exporting {len(agents)} agents...")
        agents_data = []
        for agent in agents:
            agent_data = asdict(agent)

            found, uid = _lookup(access, ("id", "-u", agent.linux_user))
            agent_data["live_uid"] = uid if found else None
            if found is False:
                output.warn(f"user '{agent.linux_user}' not found on VM")

            agent_data["grants"] = [asdict(g) for g in grants_by_agent.get(agent.name, [])]
            agents_data.append(agent_data)
        _write_json(backup_dir / "agents.json", agents_data)

        # 4. Workspaces with live GID verification
        output.info(f"Exporting {len(workspaces)} workspaces...")
        ws_data = []
        for ws in workspaces:
            ws_entry = asdict(ws)
            ws_group = ws.linux_group
            found, group = _lookup(access, ("getent", "group", ws_group))
            parts = group.split(":")
            ws_entry["live_gid"] = parts[2] if found and len(parts) > 2 else None
            if found is False:
                output.warn(f"group '{ws_group}' not found on VM")

            ws_data.append(ws_entry)
        _write_json(backup_dir / "workspaces.json", ws_data)

        # 5. Sessions
        output.info(f"Exporting {len(sessions)} sessions...")
        _write_json(backup_dir / "sessions.json", [asdict(s) for s in sessions])

        # 6. Desired overlays. This exact owner-tree projection may
        # contain plaintext environment values, so every local file
        # is created owner-only and the containing directory is 0700.
        overlay_data = [
            {
                "instance_kind": record.instance_kind,
                "instance_name": record.instance_name,
                "payload_version": record.payload.payload_version,
                "value": record.payload.value,
                "recorded_at": record.recorded_at,
            }
            for record in desired_overlays
        ]
        _write_json(backup_dir / "instance-specs.json", overlay_data)

        # 7. Applied-state evidence is non-secret, but still passes
        # through its domain codec so corrupt persisted JSON cannot
        # be laundered into an apparently valid archive.
        applied_state_data = []
        for record in applied_slices:
            from agentworks.artifacts.state import canonicalize_captures
            from agentworks.db import AppliedStateKey
            from agentworks.harness_setup.state import canonicalize_native_setup

            payload = (
                canonicalize_native_setup(record)
                if record.key is AppliedStateKey.HARNESS_NATIVE_SETUP
                else canonicalize_captures(record)
                if record.key is AppliedStateKey.ARTIFACT_INPUTS
                else canonicalize_vm_applied_slice(record)
            )
            applied_state_data.append(
                {
                    "instance_kind": record.instance_kind,
                    "instance_name": record.instance_name,
                    "key": record.key.value,
                    "payload_version": payload.payload_version,
                    "value": payload.value,
                    "operation": record.operation,
                    "recorded_at": record.recorded_at,
                }
            )
        _write_json(backup_dir / "instance-applied-state.json", applied_state_data)

        # 8. Workspace files -- single archive of all workspace paths. Ctrl-C
        # anywhere in this body ends the backup as UserAbort with a
        # backup_failed event (backup_vm); _archive_workspaces stops and
        # discards the remote archive on its way out.
        archived_paths: list[str] = []
        skipped_paths: list[str] = []
        if workspaces:
            output.info("Archiving workspace files...")
            local_archive = backup_dir / "workspaces.tar.zst"
            archived_paths, skipped_paths = _archive_workspaces(access, workspaces, local_archive)
        else:
            output.info("No VM workspaces to archive.")

        # 9. Manifest
        manifest = {
            "version": 5,
            "vm_name": vm_name,
            "timestamp": timestamp,
            "agent_count": len(agents_data),
            "workspace_count": len(ws_data),
            "session_count": len(sessions),
            "event_count": len(events),
            "instance_spec_count": len(overlay_data),
            "applied_state_count": len(applied_state_data),
            "archived_paths": archived_paths,
            "skipped_paths": skipped_paths,
        }
        _write_json(backup_dir / "manifest.json", manifest)

    db.insert_vm_event(vm_name, "backup_completed", detail=str(backup_dir))
    output.result(f"Backup complete: {format_host_path(backup_dir)}")
    return backup_dir


ARCHIVE_NAME = "workspaces.tar.zst"
"""The archive's name in the launch's work directory and in the backup directory."""

POLL_SECONDS = 5
"""How often the archive is observed while it runs."""

REPORT_SECONDS = 30
"""How often the archive's size, a transfer's progress or a lost connection is reported."""


def _archive_workspaces(
    access: VMAccess, vm_workspaces: list[WorkspaceRow], local_archive: Path
) -> tuple[list[str], list[str]]:
    """Archive every workspace path that exists into one zstd tar, detached on the VM, and download it.

    The archive runs as detached work (``agentworks.execution.detached``) in
    its launch's root-only directory, so it survives a dropped connection;
    this polls it until it ends, reporting a lost connection once, a reminder
    every 30 seconds and the recovery. Leaving before the archive is known to
    have ended, by Ctrl-C or any error, interrupts its whole process group
    first. Whatever happens once the launch was attempted, its directory is
    then discarded by the id ``new_launch`` chose before anything was sent.

    Returns (archived_paths, skipped_paths): the paths that were archived and
    those skipped because they do not exist on the VM.
    """
    detached = access.detached()
    launch = detached.new_launch()
    launched = ended = False
    try:
        valid: list[WorkspaceRow] = []
        skipped: list[str] = []
        for ws in vm_workspaces:
            if _directory_exists(access, ws.workspace_path):
                valid.append(ws)
            else:
                output.warn(f"path not found, skipping: {ws.workspace_path}")
                skipped.append(ws.workspace_path)

        if not valid:
            raise BackupError("no workspace paths exist on the VM")

        if access.run(Script("command -v zstd >/dev/null 2>&1", Shell.SH)).require_completed(access.vm_name) != 0:
            raise BackupError(
                "zstd is not installed on the VM.",
                hint="Run 'agw vm reinit' to install it.",
            )

        paths = [ws.workspace_path for ws in valid]
        total_size = _total_size(access, paths)
        if total_size is not None:
            output.detail(f"Total workspace size: {_fmt_size(total_size)} (uncompressed)")

        # zstd level 15 trades CPU for a smaller archive, which cross-workspace deduplication benefits from.
        output.detail(f"Archiving {len(valid)} workspace(s) with zstd (this may take a while)...")
        with output.section():
            output.detail(f"Remote archive: {detached_program.BASE}/{launch.id}/work/{ARCHIVE_NAME}")
            output.detail(f"Local archive:  {format_host_path(local_archive)}")

        # Every option precedes ``--``, so tar reads each path after it as a member name, never an option.
        members = [path.lstrip("/") for path in paths]
        archive = Command(
            ("tar", "--zstd", "-cf", ARCHIVE_NAME, "-C", "/", "--", *members),
            env={"ZSTD_CLEVEL": "15"},
            elevated=True,
        )
        launched = True
        detached.launch(launch, archive)
        observation = _wait_for_archive(detached, launch, access.vm_name)
        ended = True

        if observation.state is State.LOST:
            raise BackupError(
                f"the archive's process on VM '{access.vm_name}' ended without recording a status,"
                " as the VM restarting leaves it",
                hint="Run the backup again.",
            )
        if observation.state is State.ABSENT:
            raise BackupError(f"the archive's directory on VM '{access.vm_name}' disappeared (launch {launch.id})")
        tail = _decoded(observation.output_tail)
        if observation.status != 0:
            detail = f"\nOutput:\n{tail}" if tail else ""
            raise BackupError(f"tar failed (exit {observation.status}){detail}")

        archive_size = observation.work.get(ARCHIVE_NAME, 0)
        output.detail(f"Archive size: {_fmt_size(archive_size)}")
        if tail:
            output.warn("tar warnings:")
            with output.section():
                for line in tail.splitlines():
                    output.detail(line)

        output.detail("Transferring remote archive to local...")
        progress = _TransferProgress(archive_size)
        detached.download(launch, ARCHIVE_NAME, ToFile(local_archive, on_progress=progress))
        output.detail(f"Saved: {format_host_path(local_archive)} ({_fmt_size(local_archive.stat().st_size)})")
    finally:
        if launched:
            if not ended:
                _interrupt(detached, launch)
            _discard(detached, launch)

    return [ws.workspace_path for ws in valid], skipped


def _lookup(access: VMAccess, argv: tuple[str, ...]) -> tuple[bool | None, str]:
    """Whether a lookup found its name, and its output stripped; ``None`` when the request did not complete."""
    try:
        result = access.run(Command(argv))
        status = result.require_completed(access.vm_name)
    except ExecutionIncomplete:
        return None, ""
    return status == 0, result.stdout.decode("utf-8", errors="replace").strip()


def _directory_exists(access: VMAccess, path: str) -> bool:
    """Whether ``path`` is a directory on the VM, tested as root; an outcome that is not a completion raises."""
    return access.run(Command(("test", "-d", path), elevated=True)).require_completed(access.vm_name) == 0


def _total_size(access: VMAccess, paths: list[str]) -> int | None:
    """The paths' total size in bytes, counted once where they overlap, or ``None`` when ``du`` gave no total."""
    result = access.run(Command(("du", "-sbc", "--", *paths), elevated=True))
    if not isinstance(result.completion, Completed) or result.completion.status != 0:
        return None
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    size, _, label = lines[-1].partition("\t") if lines else ("", "", "")
    return int(size) if label == "total" and size.isdigit() else None


def _wait_for_archive(detached: Detached, launch: Launch, vm_name: str) -> Observation:
    """Poll the archive until it is no longer running, and return the observation that showed it.

    A poll that cannot reach the VM is not a failure: the archive keeps
    running there, so the loss is reported once, and every 30 seconds either
    a reminder or the archive's size, then the recovery once. There is no
    give-up bound; Ctrl-C ends the wait.
    """
    last_report = time.monotonic()
    connected = True
    while True:
        try:
            observation = detached.observe(launch)
        except ExecutionIncomplete as error:
            if error.result.completion not in reachability.POLLED:
                raise
            if connected:
                connected, last_report = False, time.monotonic()
                output.warn(
                    f"Lost the connection to VM '{vm_name}'; the archive keeps running there. Waiting to reconnect..."
                )
        else:
            if not connected:
                connected = True
                output.info(f"Reconnected to VM '{vm_name}'.")
            if observation.state is not State.RUNNING:
                return observation
        now = time.monotonic()
        if now - last_report >= REPORT_SECONDS:
            last_report = now
            if connected:
                output.detail(f"Archive size: {_fmt_size(observation.work.get(ARCHIVE_NAME, 0))}")
            else:
                output.detail(f"Still waiting to reconnect to VM '{vm_name}'...")
        time.sleep(POLL_SECONDS)


def _interrupt(detached: Detached, launch: Launch) -> None:
    """Stop the archive's process group; a failure, or a second Ctrl-C, warns, since the backup is already ending."""
    try:
        output.warn("Stopping the remote archive...")
        observation = detached.interrupt(launch)
    except (AgentworksError, KeyboardInterrupt) as error:
        output.warn(f"Could not stop the remote archive (launch {launch.id}): {str(error) or 'interrupted'}")
        return
    if observation.state is State.RUNNING:
        output.warn(f"The remote archive is still running (launch {launch.id}).")


def _discard(detached: Detached, launch: Launch) -> None:
    """Remove the launch's directory; a failure, or a second Ctrl-C, warns."""
    try:
        detached.discard(launch)
    except (AgentworksError, KeyboardInterrupt) as error:
        output.warn(
            f"Could not remove the archive's directory on the VM (launch {launch.id}): {str(error) or 'interrupted'}."
            " A later backup on this VM removes it once the archive has ended and is a week old."
        )


class _TransferProgress:
    """The download's progress callback: reports the bytes written at most every 30 seconds."""

    def __init__(self, total: int) -> None:
        self._total = total
        self._last_report = time.monotonic()

    def __call__(self, written: int) -> None:
        now = time.monotonic()
        if now - self._last_report < REPORT_SECONDS:
            return
        self._last_report = now
        if self._total > 0:
            output.detail(f"Transfer: {_fmt_size(written)} / {_fmt_size(self._total)} ({written / self._total:.0%})")
        else:
            output.detail(f"Transfer: {_fmt_size(written)}")


def _decoded(tail: bytes) -> str:
    """The last ten lines of the archive's output, decoded and safe to show on a terminal."""
    lines = [line for line in tail.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return sanitize_terminal_output("\n".join(lines[-10:]))


def _fmt_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string."""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024**3):.1f} GB"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024**2):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _write_json(path: Path, data: object) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(data, indent=2, default=str) + "\n")
    path.chmod(0o600)


def _host_supports_private_backup_permissions() -> bool:
    return os.name != "nt"


def _raise_windows_overlay_backup_error(vm_name: str) -> NoReturn:
    raise BackupError(
        "VM backups containing instance specs are not supported on Windows",
        entity_kind="vm",
        entity_name=vm_name,
        hint=(
            "Run the backup from a POSIX host; private-DACL export for plaintext "
            "instance-spec environment values is not implemented yet."
        ),
    )


def _create_backup_directory(path: Path, vm_name: str) -> None:
    """Create an owner-only backup destination and refuse any collision."""
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=False)
    except FileExistsError as error:
        raise BackupError(
            f"backup destination already exists: {format_host_path(path)}",
            entity_kind="vm",
            entity_name=vm_name,
        ) from error
    path.chmod(0o700)
