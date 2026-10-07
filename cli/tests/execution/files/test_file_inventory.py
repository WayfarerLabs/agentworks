"""Bounded private Linux directory inventory behavior."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

import agentworks.execution._file_inventory as inventory_module
from agentworks.execution._file_inventory import (
    FileInventoryEntry,
    FileInventoryError,
    FileInventoryFailureKind,
    encode_inventory,
    inventory_directory,
)
from agentworks.execution._file_objects import FileKind, revision_kind

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux directory inventory")

_LARGE_LIMIT = 4 * 1024 * 1024


def _open_directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _inventory(
    root_fd: int,
    *,
    max_entries: int = 32,
    max_depth: int = 8,
    max_encoded_bytes: int = _LARGE_LIMIT,
    expires_at: float | None = None,
) -> tuple[FileInventoryEntry, ...]:
    if expires_at is None:
        expires_at = time.monotonic() + 60.0
    return inventory_directory(
        root_fd,
        max_entries=max_entries,
        max_depth=max_depth,
        max_encoded_bytes=max_encoded_bytes,
        expires_at=expires_at,
    )


def _failure(root_fd: int, **kwargs: object) -> FileInventoryError:
    with pytest.raises(FileInventoryError) as raised:
        _inventory(root_fd, **kwargs)  # type: ignore[arg-type]
    return raised.value


def test_inventory_reports_nested_tree_to_requested_depth(tmp_path: Path) -> None:
    (tmp_path / "root-file").write_bytes(b"root")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "child").write_bytes(b"child")
    deeper = nested / "deeper"
    deeper.mkdir()
    (deeper / "hidden").write_bytes(b"hidden")
    root_fd = _open_directory(tmp_path)
    try:
        shallow = _inventory(root_fd, max_depth=1)
        medium = _inventory(root_fd, max_depth=2)
    finally:
        os.close(root_fd)

    assert [entry.relative_path for entry in shallow] == ["nested", "root-file"]
    assert [entry.relative_path for entry in medium] == [
        "nested",
        "nested/child",
        "nested/deeper",
        "root-file",
    ]
    assert revision_kind(shallow[0].revision) is FileKind.DIRECTORY
    assert all(entry.revision.digest is None for entry in medium)


def test_none_deadline_is_an_explicit_unbounded_inventory(tmp_path: Path) -> None:
    (tmp_path / "regular").write_bytes(b"content")
    root_fd = _open_directory(tmp_path)
    try:
        entries = inventory_directory(
            root_fd,
            max_entries=1,
            max_depth=1,
            max_encoded_bytes=_LARGE_LIMIT,
            expires_at=None,
        )
    finally:
        os.close(root_fd)
    assert [entry.relative_path for entry in entries] == ["regular"]


def test_inventory_sorts_relative_utf8_paths_bytewise(tmp_path: Path) -> None:
    names = ["z", "snowman-☃", "e-acute-é", "alpha"]
    for name in names:
        (tmp_path / name).write_bytes(b"")
    root_fd = _open_directory(tmp_path)
    try:
        entries = _inventory(root_fd, max_depth=1)
    finally:
        os.close(root_fd)

    assert [entry.relative_path for entry in entries] == sorted(names, key=lambda name: name.encode("utf-8"))


def test_boundary_directory_is_not_opened_or_inspected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    boundary = tmp_path / "boundary"
    boundary.mkdir()
    os.mkfifo(boundary / "forbidden-fifo")
    (boundary / "unreadable").write_bytes(b"private")
    boundary.chmod(0)
    root_fd = _open_directory(tmp_path)
    monkeypatch.setattr(
        inventory_module,
        "_open_directory",
        lambda *_args: pytest.fail("boundary directory was opened"),
    )
    try:
        entries = _inventory(root_fd, max_depth=1)
        os.fstat(root_fd)
    finally:
        boundary.chmod(0o700)
        os.close(root_fd)

    assert [entry.relative_path for entry in entries] == ["boundary"]


def test_exact_entry_and_encoded_size_limits(tmp_path: Path) -> None:
    (tmp_path / "first").write_bytes(b"one")
    (tmp_path / "second").write_bytes(b"two")
    root_fd = _open_directory(tmp_path)
    try:
        entries = _inventory(root_fd, max_entries=2)
        exact_size = len(encode_inventory(entries))
        assert _inventory(root_fd, max_entries=2, max_encoded_bytes=exact_size) == entries
        assert _failure(root_fd, max_entries=1).kind is FileInventoryFailureKind.LIMIT
        assert _failure(root_fd, max_entries=2, max_encoded_bytes=exact_size - 1).kind is FileInventoryFailureKind.LIMIT
    finally:
        os.close(root_fd)


def test_encoded_size_is_exact_canonical_json_including_framing(tmp_path: Path) -> None:
    (tmp_path / "snowman-☃").write_bytes(b"content")
    root_fd = _open_directory(tmp_path)
    try:
        entries = _inventory(root_fd, max_depth=1)
    finally:
        os.close(root_fd)

    encoded = encode_inventory(entries)
    decoded = json.loads(encoded)
    assert encoded.startswith(b"[") and encoded.endswith(b"]")
    assert b" " not in encoded
    assert decoded[0]["relative_path"] == "snowman-☃"
    assert set(decoded[0]) == {
        "relative_path",
        "device",
        "inode",
        "mode",
        "link_count",
        "uid",
        "gid",
        "size",
        "modified_ns",
        "changed_ns",
    }
    assert "digest" not in decoded[0]


def test_malformed_filename_bytes_are_refused(tmp_path: Path) -> None:
    root_fd = _open_directory(tmp_path)
    descriptor = os.open(b"\xff", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd)
    os.close(descriptor)
    try:
        error = _failure(root_fd)
    finally:
        os.close(root_fd)
    assert error.kind is FileInventoryFailureKind.UNSUPPORTED


def test_inventory_observes_regular_directory_and_socket(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_bytes(b"private content")
    (tmp_path / "directory").mkdir()
    socket_path = tmp_path / "socket"
    root_fd = _open_directory(tmp_path)
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(f"/proc/self/fd/{root_fd}/{socket_path.name}")
        entries = _inventory(root_fd, max_depth=1)
    finally:
        os.close(root_fd)
        listener.close()

    assert {entry.relative_path: revision_kind(entry.revision) for entry in entries} == {
        "directory": FileKind.DIRECTORY,
        "regular": FileKind.REGULAR,
        "socket": FileKind.SOCKET,
    }
    assert next(entry for entry in entries if entry.relative_path == "regular").revision.stat.size == len(
        b"private content"
    )


def test_inventory_reads_no_regular_file_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "regular"
    target.write_bytes(b"private content")
    target.chmod(0)
    root_fd = _open_directory(tmp_path)
    monkeypatch.setattr(os, "read", lambda *_args: pytest.fail("file content was read"))
    try:
        entries = _inventory(root_fd, max_depth=1)
    finally:
        os.close(root_fd)
    assert [entry.relative_path for entry in entries] == ["regular"]


@pytest.mark.parametrize("object_kind", ["symlink", "hardlink", "fifo"])
def test_inventory_refuses_links_and_unsupported_special_objects(tmp_path: Path, object_kind: str) -> None:
    target = tmp_path / "target"
    if object_kind == "symlink":
        target.symlink_to(tmp_path / "missing")
    elif object_kind == "hardlink":
        target.write_bytes(b"content")
        os.link(target, tmp_path / "other")
    else:
        os.mkfifo(target)
    root_fd = _open_directory(tmp_path)
    try:
        error = _failure(root_fd)
    finally:
        os.close(root_fd)
    assert error.kind is FileInventoryFailureKind.UNSUPPORTED


def test_inventory_refuses_descendant_proc_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    if not Path("/proc").is_dir():
        pytest.skip("procfs fixture is unavailable")

    class _ProcEntry:
        name = "proc"

    root_fd = _open_directory(Path("/"))

    class _OnlyProc:
        def __iter__(self) -> _OnlyProc:
            return self

        def __next__(self) -> _ProcEntry:
            if getattr(self, "_used", False):
                raise StopIteration
            self._used = True
            return _ProcEntry()

        def close(self) -> None:
            pass

    monkeypatch.setattr(inventory_module, "_scandir", lambda descriptor: _OnlyProc())
    try:
        error = _failure(root_fd, max_depth=1)
    finally:
        os.close(root_fd)
    assert error.kind is FileInventoryFailureKind.UNSUPPORTED


def test_inventory_refuses_device_without_access(monkeypatch: pytest.MonkeyPatch) -> None:
    if not Path("/dev/null").exists():
        pytest.skip("device fixture is unavailable")

    class _DeviceEntry:
        name = "null"

    class _OnlyDevice:
        def __init__(self) -> None:
            self.used = False

        def __iter__(self) -> _OnlyDevice:
            return self

        def __next__(self) -> _DeviceEntry:
            if self.used:
                raise StopIteration
            self.used = True
            return _DeviceEntry()

        def close(self) -> None:
            pass

    root_fd = _open_directory(Path("/dev"))
    monkeypatch.setattr(inventory_module, "_scandir", lambda _descriptor: _OnlyDevice())
    try:
        error = _failure(root_fd, max_depth=1)
        os.fstat(root_fd)
    finally:
        os.close(root_fd)
    assert error.kind is FileInventoryFailureKind.UNSUPPORTED


def test_disappearance_after_enumeration_is_a_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"content")
    root_fd = _open_directory(tmp_path)
    real_observe = inventory_module._observe

    def disappearing_observe(parent_fd: int, name: str, expires_at: float | None):
        target.unlink()
        return real_observe(parent_fd, name, expires_at)

    monkeypatch.setattr(inventory_module, "_observe", disappearing_observe)
    try:
        error = _failure(root_fd)
    finally:
        os.close(root_fd)
    assert error.kind is FileInventoryFailureKind.CONFLICT


def test_replaced_directory_during_reopen_is_a_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.mkdir()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    root_fd = _open_directory(tmp_path)
    real_open = inventory_module._open_directory

    def replacing_open(parent_fd: int, name: str) -> int | None:
        if name == target.name:
            target.rmdir()
            replacement.rename(target)
        return real_open(parent_fd, name)

    monkeypatch.setattr(inventory_module, "_open_directory", replacing_open)
    try:
        error = _failure(root_fd, max_depth=2)
    finally:
        os.close(root_fd)
    assert error.kind is FileInventoryFailureKind.CONFLICT


def test_owned_child_descriptor_is_closed_on_nested_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    child = tmp_path / "child"
    child.mkdir()
    os.mkfifo(child / "fifo")
    root_fd = _open_directory(tmp_path)
    closed: list[int] = []
    real_close = os.close

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(inventory_module, "_close", recording_close)
    try:
        error = _failure(root_fd, max_depth=2)
        os.fstat(root_fd)
    finally:
        os.close(root_fd)

    assert error.kind is FileInventoryFailureKind.UNSUPPORTED
    assert len(closed) == 1
    with pytest.raises(OSError):
        os.fstat(closed[0])


def test_expired_inventory_refuses_before_enumeration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_fd = _open_directory(tmp_path)
    monkeypatch.setattr(inventory_module, "_scandir", lambda *_args: pytest.fail("directory was enumerated"))
    try:
        error = _failure(root_fd, expires_at=0.0)
        os.fstat(root_fd)
    finally:
        os.close(root_fd)
    assert error.kind is FileInventoryFailureKind.DEADLINE


def test_deadline_during_traversal_returns_no_partial_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "first").write_bytes(b"")
    (tmp_path / "second").write_bytes(b"")
    root_fd = _open_directory(tmp_path)
    now = 0.0
    encode_record = inventory_module._encode_record

    def encode_after_deadline(entry: FileInventoryEntry) -> bytes:
        nonlocal now
        record = encode_record(entry)
        now = 2.0
        return record

    monkeypatch.setattr(time, "monotonic", lambda: now)
    monkeypatch.setattr(inventory_module, "_encode_record", encode_after_deadline)
    try:
        error = _failure(root_fd, expires_at=1.0)
        os.fstat(root_fd)
    finally:
        os.close(root_fd)
    assert error.kind is FileInventoryFailureKind.DEADLINE


def test_entry_limit_stops_unknown_total_before_further_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "first").write_bytes(b"")
    (tmp_path / "second").write_bytes(b"")
    root_fd = _open_directory(tmp_path)

    class _Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    class _BoundedScan:
        def __init__(self) -> None:
            self._entries = iter((_Entry("first"), _Entry("second")))
            self.closed = False

        def __iter__(self) -> _BoundedScan:
            return self

        def __next__(self) -> _Entry:
            return next(self._entries)

        def close(self) -> None:
            self.closed = True

    scan = _BoundedScan()
    monkeypatch.setattr(inventory_module, "_scandir", lambda _descriptor: scan)
    try:
        error = _failure(root_fd, max_entries=1)
    finally:
        os.close(root_fd)
    assert error.kind is FileInventoryFailureKind.LIMIT
    assert scan.closed


def test_overlong_name_refuses_before_object_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_fd = _open_directory(tmp_path)

    class _Entry:
        name = "a" * 256

    class _Scan:
        def __init__(self) -> None:
            self.used = False

        def __iter__(self) -> _Scan:
            return self

        def __next__(self) -> _Entry:
            if self.used:
                raise StopIteration
            self.used = True
            return _Entry()

        def close(self) -> None:
            pass

    monkeypatch.setattr(inventory_module, "_scandir", lambda _descriptor: _Scan())
    monkeypatch.setattr(
        inventory_module,
        "stat_file_object",
        lambda *_args, **_kwargs: pytest.fail("overlong name was observed"),
    )
    try:
        error = _failure(root_fd)
    finally:
        os.close(root_fd)
    assert error.kind is FileInventoryFailureKind.LIMIT
