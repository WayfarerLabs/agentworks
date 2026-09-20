"""Linux kernel-backed path confinement for private file operations."""

from __future__ import annotations

import errno
import os
import platform
import sys
from pathlib import Path

import pytest

import agentworks.execution._file_paths as paths_module
from agentworks.execution._file_paths import (
    ConfinedOpenError,
    ConfinedOpenFailure,
    open_linux_confined,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="openat2 is Linux-specific")

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_root(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _failure(root_fd: int, relative_path: str) -> ConfinedOpenError:
    with pytest.raises(ConfinedOpenError) as raised:
        open_linux_confined(root_fd, relative_path, _READ_FLAGS)
    return raised.value


def test_actual_openat2_opens_nested_regular_file_and_borrows_root(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "file"
    target.parent.mkdir()
    target.write_bytes(b"content")
    root_fd = _open_root(tmp_path)
    descriptor: int | None = None
    try:
        descriptor = open_linux_confined(root_fd, "nested/file", _READ_FLAGS)
        assert descriptor is not None
        assert os.read(descriptor, 1024) == b"content"
        os.fstat(root_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(root_fd)


def test_actual_openat2_reports_only_enoent_as_absence(tmp_path: Path) -> None:
    (tmp_path / "not-directory").write_bytes(b"content")
    root_fd = _open_root(tmp_path)
    try:
        assert open_linux_confined(root_fd, "missing", _READ_FLAGS) is None
        assert open_linux_confined(root_fd, "missing/leaf", _READ_FLAGS) is None
        error = _failure(root_fd, "not-directory/leaf")
    finally:
        os.close(root_fd)
    assert error.kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT


def test_actual_openat2_refuses_ancestor_and_leaf_symlinks(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "file").write_bytes(b"content")
    (tmp_path / "ancestor-link").symlink_to(directory, target_is_directory=True)
    (tmp_path / "leaf-link").symlink_to(directory / "file")
    root_fd = _open_root(tmp_path)
    try:
        assert _failure(root_fd, "ancestor-link/file").kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT
        assert _failure(root_fd, "leaf-link").kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT
    finally:
        os.close(root_fd)


def test_actual_openat2_refuses_beneath_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").write_bytes(b"content")
    root_fd = _open_root(root)
    try:
        assert _failure(root_fd, "../outside").kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT
        assert _failure(root_fd, str(tmp_path / "outside")).kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT
    finally:
        os.close(root_fd)


def test_actual_openat2_refuses_procfs_magic_link() -> None:
    descriptor_directory = Path("/proc/self/fd")
    if not descriptor_directory.is_dir():
        pytest.skip("procfs descriptor fixture is unavailable")
    proc_fd = _open_root(descriptor_directory)
    target_fd = _open_root(Path("/tmp"))
    try:
        error = _failure(proc_fd, str(target_fd))
    finally:
        os.close(target_fd)
        os.close(proc_fd)
    assert error.kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT


def test_actual_openat2_refuses_descendant_mount() -> None:
    if not Path("/proc/version").is_file():
        pytest.skip("procfs mount fixture is unavailable")
    root_fd = _open_root(Path("/"))
    try:
        error = _failure(root_fd, "proc/version")
    finally:
        os.close(root_fd)
    assert error.kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT


def test_kernel_beneath_race_refusal_is_a_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_fd = _open_root(tmp_path)
    monkeypatch.setattr(paths_module, "_linux_syscall", lambda *_args: (None, errno.EAGAIN))
    try:
        error = _failure(root_fd, "file")
    finally:
        os.close(root_fd)
    assert error.kind is ConfinedOpenFailure.CONFLICT


def test_unavailable_syscall_refuses_without_fallback_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"unchanged")
    root_fd = _open_root(tmp_path)
    monkeypatch.setattr(paths_module, "_linux_syscall", lambda *_args: (None, errno.ENOSYS))
    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: pytest.fail("fallback open was used"))
    try:
        error = _failure(root_fd, "private-name")
    finally:
        os.close(root_fd)

    assert error.kind is ConfinedOpenFailure.IO
    assert error.args == (ConfinedOpenFailure.IO.value,)
    assert "private-name" not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert target.read_bytes() == b"unchanged"


def test_unsupported_linux_architecture_refuses_before_syscall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_fd = _open_root(tmp_path)
    monkeypatch.setattr(platform, "machine", lambda: "unsupported")
    monkeypatch.setattr(paths_module, "_linux_syscall", lambda *_args: pytest.fail("syscall was attempted"))
    try:
        error = _failure(root_fd, "file")
    finally:
        os.close(root_fd)
    assert error.kind is ConfinedOpenFailure.IO
