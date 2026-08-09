"""Requested-parent filesystem preflight regressions for database snapshots."""

from __future__ import annotations

import errno
import os
from pathlib import Path, PureWindowsPath
from time import monotonic
from typing import cast

import pytest


def _database_files(db_path: Path) -> tuple[Path, Path, Path]:
    return db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")


@pytest.mark.parametrize("database_state", ["absent", "active"])
@pytest.mark.parametrize("error_number", [errno.ENOSYS, errno.EOPNOTSUPP])
def test_preflight_probes_resolved_requested_parent_before_source_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_state: str,
    error_number: int,
) -> None:
    """Target-filesystem unavailability closes every fd before source access."""
    import agentworks.db.inspection as inspection_module
    from agentworks.db import Database
    from agentworks.errors import DatabaseInspectionUnavailable

    real_parent = tmp_path / "real-parent"
    target_parent = real_parent / "target-filesystem"
    target_parent.mkdir(parents=True)
    alias_parent = tmp_path / "component-alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    requested_path = alias_parent / target_parent.name / "operator-private.db"
    real_path = target_parent / requested_path.name

    writer: Database | None = None
    if database_state == "active":
        writer = Database(real_path)
        writer.set_setting("system_slug", "active-target-filesystem")
        assert all(path.exists() for path in _database_files(real_path))
    else:
        assert not real_path.exists()

    original_open = os.open
    original_close = os.close
    original_resolve = Path.resolve
    target_directory_fd: int | None = None
    target_probes: list[str] = []
    open_directory_fds: set[int] = set()
    requested_entry_access: list[Path] = []
    requested_entry_resolutions: list[Path] = []
    source_entry_access: list[str] = []

    def unavailable_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal target_directory_fd
        if path == "." and kwargs.get("dir_fd") == target_directory_fd:
            target_probes.append(path)
            raise OSError(error_number, "operator-private-target-probe")
        descriptor = original_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            open_directory_fds.add(descriptor)
            if path == target_parent.name:
                target_directory_fd = descriptor
        return descriptor

    def record_close(descriptor: int) -> None:
        open_directory_fds.discard(descriptor)
        original_close(descriptor)

    def unexpected_requested_entry(path: Path) -> bool:
        requested_entry_access.append(path)
        return True

    def record_resolve(path: Path, strict: bool = False) -> Path:
        if path == requested_path:
            requested_entry_resolutions.append(path)
        return original_resolve(path, strict=strict)

    def unexpected_source_entry(name: str, directory_fd: int) -> int:
        source_entry_access.append(name)
        raise AssertionError(f"source entry opened through fd {directory_fd}")

    monkeypatch.setattr(inspection_module, "_requested_entry_exists", unexpected_requested_entry)
    monkeypatch.setattr(inspection_module, "_open_source", unexpected_source_entry)
    monkeypatch.setattr(Path, "resolve", record_resolve)
    monkeypatch.setattr(os, "open", unavailable_open)
    monkeypatch.setattr(os, "close", record_close)
    monkeypatch.setattr(os, "supports_dir_fd", frozenset({unavailable_open}))

    started = monotonic()
    try:
        with pytest.raises(DatabaseInspectionUnavailable) as raised, Database.inspection_snapshot(requested_path):
            pytest.fail("target-filesystem unavailability must not yield a snapshot")
    finally:
        if writer is not None:
            writer.close()

    assert monotonic() - started < 1
    assert str(raised.value) == "secure database inspection is unavailable on this host"
    assert "operator-private" not in str(raised.value)
    assert target_probes == ["."]
    assert requested_entry_access == []
    assert requested_entry_resolutions == []
    assert source_entry_access == []
    assert open_directory_fds == set()


def test_drive_relative_requested_path_is_rejected() -> None:
    """A drive-relative path cannot be rooted safely against the process cwd."""
    import agentworks.db.inspection as inspection_module

    requested_path = cast("Path", PureWindowsPath("C:operator-private.db"))

    with pytest.raises(inspection_module._UnsupportedSnapshotEntry):
        inspection_module._rooted_lexical_requested_path(requested_path)
