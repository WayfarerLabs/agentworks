"""Filesystem observations for the private cooperative snapshot reader."""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import agentworks.execution._file_snapshot as snapshot_module
from agentworks.execution._file_paths import open_linux_confined
from agentworks.execution._file_snapshot import (
    FileSnapshot,
    SnapshotFailureKind,
    SnapshotReadError,
    read_snapshot,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="descriptor-relative POSIX observation")


def _open_root(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _failure_kind(root_fd: int, relative_path: str, max_bytes: int = 1024) -> SnapshotFailureKind:
    with pytest.raises(SnapshotReadError) as raised:
        read_snapshot(root_fd, relative_path, max_bytes)
    return raised.value.kind


def test_regular_binary_snapshot_binds_bytes_stat_and_digest(tmp_path: Path) -> None:
    content = b"\x00private\xffbytes\n"
    target = tmp_path / "nested" / "payload.bin"
    target.parent.mkdir()
    target.write_bytes(content)
    root_fd = _open_root(tmp_path)
    try:
        result = read_snapshot(root_fd, "nested/payload.bin", len(content))
    finally:
        os.close(root_fd)

    assert isinstance(result, FileSnapshot)
    assert result.data == content
    assert result.digest == hashlib.sha256(content).digest()
    assert result.stat.size == len(content)
    assert result.stat.link_count == 1
    assert result.stat.inode == target.stat().st_ino
    representation = repr(result)
    assert repr(content) not in representation
    assert repr(result.digest) not in representation
    with pytest.raises(FrozenInstanceError):
        result.data = b"changed"  # type: ignore[misc]


@pytest.mark.parametrize("relative_path", ["missing", "missing/leaf", "present/missing"])
def test_absence_is_the_only_none_result(tmp_path: Path, relative_path: str) -> None:
    (tmp_path / "present").mkdir()
    root_fd = _open_root(tmp_path)
    try:
        assert read_snapshot(root_fd, relative_path, 1) is None
    finally:
        os.close(root_fd)


@pytest.mark.parametrize(
    "relative_path",
    ["", "/absolute", ".", "..", "a/./b", "a/../b", "a//b", "a/", "nul\x00name"],
)
def test_malformed_relative_paths_are_rejected_before_io(tmp_path: Path, relative_path: str) -> None:
    root_fd = _open_root(tmp_path)
    os.close(root_fd)
    with pytest.raises(ValueError):
        read_snapshot(root_fd, relative_path, 1)


def test_malformed_relative_path_never_reaches_confined_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_fd = _open_root(tmp_path)
    monkeypatch.setattr(snapshot_module, "_open_at", lambda *_args, **_kwargs: pytest.fail("path was opened"))
    try:
        with pytest.raises(ValueError):
            read_snapshot(root_fd, "nested/../file", 1)
    finally:
        os.close(root_fd)


@pytest.mark.parametrize("max_bytes", [0, -1, True, 1.0, float("inf")])
def test_byte_bound_must_be_a_positive_integer(tmp_path: Path, max_bytes: object) -> None:
    root_fd = _open_root(tmp_path)
    os.close(root_fd)
    with pytest.raises(ValueError):
        read_snapshot(root_fd, "file", max_bytes)  # type: ignore[arg-type]


def test_ancestor_and_leaf_links_are_refused(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "file").write_bytes(b"content")
    (tmp_path / "ancestor-link").symlink_to(directory, target_is_directory=True)
    (tmp_path / "leaf-link").symlink_to(directory / "file")
    root_fd = _open_root(tmp_path)
    try:
        assert _failure_kind(root_fd, "ancestor-link/file") is SnapshotFailureKind.UNSUPPORTED_OBJECT
        assert _failure_kind(root_fd, "leaf-link") is SnapshotFailureKind.UNSUPPORTED_OBJECT
    finally:
        os.close(root_fd)


def test_multiply_linked_regular_file_is_refused_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"content")
    os.link(target, tmp_path / "second-name")
    root_fd = _open_root(tmp_path)
    monkeypatch.setattr(snapshot_module, "_open_at", lambda *_args, **_kwargs: pytest.fail("hard link was opened"))
    try:
        assert _failure_kind(root_fd, "file") is SnapshotFailureKind.UNSUPPORTED_OBJECT
    finally:
        os.close(root_fd)


def test_fifo_directory_and_socket_are_refused_without_opening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    os.mkfifo(tmp_path / "fifo")
    (tmp_path / "directory").mkdir()
    socket_path = tmp_path / "socket"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    root_fd = _open_root(tmp_path)
    monkeypatch.setattr(snapshot_module, "_open_at", lambda *_args, **_kwargs: pytest.fail("special object was opened"))
    try:
        for name in ("fifo", "directory", "socket"):
            assert _failure_kind(root_fd, name) is SnapshotFailureKind.UNSUPPORTED_OBJECT
    finally:
        os.close(root_fd)
        listener.close()


def test_device_is_refused_without_opening(monkeypatch: pytest.MonkeyPatch) -> None:
    device = Path("/dev/null")
    if not device.exists() or not stat.S_ISCHR(device.stat().st_mode):
        pytest.skip("known character-device fixture is unavailable")
    root_fd = _open_root(device.parent)
    monkeypatch.setattr(snapshot_module, "_open_at", lambda *_args, **_kwargs: pytest.fail("device was opened"))
    try:
        assert _failure_kind(root_fd, device.name) is SnapshotFailureKind.UNSUPPORTED_OBJECT
    finally:
        os.close(root_fd)


def test_regular_leaf_open_uses_nonblocking_nofollow_noctty_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "file").write_bytes(b"content")
    root_fd = _open_root(tmp_path)
    observed_flags: list[int] = []

    if sys.platform == "linux":

        def recording_confined_open(root_fd: int, path: str, flags: int) -> int | None:
            observed_flags.append(flags)
            return open_linux_confined(root_fd, path, flags)

        monkeypatch.setattr("agentworks.execution._file_snapshot.open_linux_confined", recording_confined_open)
    else:
        original_open = os.open

        def recording_open(path: str, flags: int, *, dir_fd: int) -> int:
            observed_flags.append(flags)
            return original_open(path, flags, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", recording_open)
    try:
        assert read_snapshot(root_fd, "file", 1024) is not None
    finally:
        os.close(root_fd)

    required = os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_NOCTTY", 0)
    assert observed_flags and observed_flags[0] & required == required


def test_limit_refuses_oversize_file_without_a_partial_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "file").write_bytes(b"oversize")
    root_fd = _open_root(tmp_path)
    monkeypatch.setattr(snapshot_module, "_read", lambda *_: pytest.fail("known oversize file was read"))
    try:
        assert _failure_kind(root_fd, "file", 3) is SnapshotFailureKind.LIMIT
    finally:
        os.close(root_fd)


def test_read_chunks_never_exceed_64_kib_and_no_smaller_total_cap_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = os.urandom(3 * 64 * 1024 + 17)
    (tmp_path / "file").write_bytes(content)
    root_fd = _open_root(tmp_path)
    original_read = snapshot_module._read
    requests: list[int] = []

    def recording_read(descriptor: int, size: int) -> bytes:
        requests.append(size)
        return original_read(descriptor, size)

    monkeypatch.setattr(snapshot_module, "_read", recording_read)
    try:
        result = read_snapshot(root_fd, "file", len(content) + 10)
    finally:
        os.close(root_fd)

    assert result is not None and result.data == content
    assert len(requests) > 3
    assert max(requests) <= 64 * 1024


def test_growth_beyond_the_bound_is_a_limit_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"1234")
    root_fd = _open_root(tmp_path)
    original_read = snapshot_module._read
    first_read = True

    def growing_read(descriptor: int, size: int) -> bytes:
        nonlocal first_read
        content = original_read(descriptor, size)
        if first_read:
            first_read = False
            with target.open("ab") as stream:
                stream.write(b"5")
                stream.flush()
                os.fsync(stream.fileno())
        return content

    monkeypatch.setattr(snapshot_module, "_read", growing_read)
    try:
        assert _failure_kind(root_fd, "file", 4) is SnapshotFailureKind.LIMIT
    finally:
        os.close(root_fd)


def test_observed_in_place_mutation_is_a_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"original")
    root_fd = _open_root(tmp_path)
    original_read = snapshot_module._read
    first_read = True

    def mutating_read(descriptor: int, size: int) -> bytes:
        nonlocal first_read
        content = original_read(descriptor, size)
        if first_read:
            first_read = False
            target.write_bytes(b"new and longer content")
        return content

    monkeypatch.setattr(snapshot_module, "_read", mutating_read)
    try:
        assert _failure_kind(root_fd, "file", 1024) is SnapshotFailureKind.CONFLICT
    finally:
        os.close(root_fd)


def test_observed_name_replacement_is_a_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "file"
    replacement = tmp_path / "replacement"
    target.write_bytes(b"original")
    replacement.write_bytes(b"replacement")
    root_fd = _open_root(tmp_path)
    original_read = snapshot_module._read
    first_read = True

    def replacing_read(descriptor: int, size: int) -> bytes:
        nonlocal first_read
        content = original_read(descriptor, size)
        if first_read:
            first_read = False
            os.replace(replacement, target)
        return content

    monkeypatch.setattr(snapshot_module, "_read", replacing_read)
    try:
        assert _failure_kind(root_fd, "file", 1024) is SnapshotFailureKind.CONFLICT
    finally:
        os.close(root_fd)


def test_replacement_between_leaf_observation_and_open_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file"
    replacement = tmp_path / "replacement"
    target.write_bytes(b"original")
    replacement.write_bytes(b"different")
    root_fd = _open_root(tmp_path)
    original_open = snapshot_module._open_at

    def replacing_open(parent_fd: int, name: str, *, directory: bool) -> int | None:
        if not directory:
            os.replace(replacement, target)
        return original_open(parent_fd, name, directory=directory)

    monkeypatch.setattr(snapshot_module, "_open_at", replacing_open)
    try:
        assert _failure_kind(root_fd, "file", 1024) is SnapshotFailureKind.CONFLICT
    finally:
        os.close(root_fd)


@pytest.mark.skipif(sys.platform != "linux", reason="real procfs boundary is Linux-specific")
def test_descendant_procfs_device_boundary_is_refused() -> None:
    if not Path("/proc/version").is_file():
        pytest.skip("procfs fixture is unavailable")
    root_fd = _open_root(Path("/"))
    try:
        assert _failure_kind(root_fd, "proc/version", 4096) is SnapshotFailureKind.UNSUPPORTED_OBJECT
    finally:
        os.close(root_fd)


def test_borrowed_root_survives_and_all_descendant_descriptors_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "file").write_bytes(b"content")
    root_fd = _open_root(tmp_path)
    original_open = snapshot_module._open_at
    original_close = snapshot_module._close
    owned: set[int] = set()

    def tracking_open(parent_fd: int, name: str, *, directory: bool) -> int | None:
        descriptor = original_open(parent_fd, name, directory=directory)
        if descriptor is None:
            return None
        assert descriptor != root_fd
        owned.add(descriptor)
        return descriptor

    def tracking_close(descriptor: int) -> None:
        assert descriptor in owned
        original_close(descriptor)
        owned.remove(descriptor)

    monkeypatch.setattr(snapshot_module, "_open_at", tracking_open)
    monkeypatch.setattr(snapshot_module, "_close", tracking_close)
    try:
        assert read_snapshot(root_fd, "one/two/file", 1024) is not None
        assert not owned
        os.fstat(root_fd)
    finally:
        os.close(root_fd)


def test_base_exception_during_directory_inspection_closes_owned_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "directory").mkdir()
    root_fd = _open_root(tmp_path)
    original_open = snapshot_module._open_at
    original_close = snapshot_module._close
    original_fstat = snapshot_module._fstat
    owned: set[int] = set()

    def tracking_open(parent_fd: int, name: str, *, directory: bool) -> int | None:
        descriptor = original_open(parent_fd, name, directory=directory)
        if descriptor is not None:
            owned.add(descriptor)
        return descriptor

    def tracking_close(descriptor: int) -> None:
        original_close(descriptor)
        owned.remove(descriptor)

    def interrupting_fstat(descriptor: int) -> os.stat_result:
        if descriptor != root_fd:
            raise SystemExit
        return original_fstat(descriptor)

    monkeypatch.setattr(snapshot_module, "_open_at", tracking_open)
    monkeypatch.setattr(snapshot_module, "_close", tracking_close)
    monkeypatch.setattr(snapshot_module, "_fstat", interrupting_fstat)
    try:
        with pytest.raises(SystemExit):
            read_snapshot(root_fd, "directory/file", 1024)
        assert not owned
        os.fstat(root_fd)
    finally:
        os.close(root_fd)


def test_io_failure_diagnostic_retains_no_os_error_or_input(tmp_path: Path) -> None:
    root_fd = _open_root(tmp_path)
    os.close(root_fd)
    secret_path = "private-name"
    with pytest.raises(SnapshotReadError) as raised:
        read_snapshot(root_fd, secret_path, 1024)

    error = raised.value
    assert error.kind is SnapshotFailureKind.IO
    assert error.args == (SnapshotFailureKind.IO.value,)
    assert secret_path not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
