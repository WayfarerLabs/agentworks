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
    normalized_relative_path,
    normalized_root,
    open_linux_confined,
    open_linux_root,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="openat2 is Linux-specific")

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_root(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _failure(root_fd: int, relative_path: str) -> ConfinedOpenError:
    with pytest.raises(ConfinedOpenError) as raised:
        open_linux_confined(root_fd, relative_path, _READ_FLAGS)
    return raised.value


@pytest.mark.parametrize("path", ["/", "/root", "/root/nested", "/snowman-☃"])
def test_normalized_root_accepts_canonical_absolute_paths(path: str) -> None:
    assert normalized_root(path)


@pytest.mark.parametrize("path", ["", "relative", "//", "/root/", "/root//leaf", "/./leaf", "/../leaf", "/nul\0"])
def test_normalized_root_rejects_every_noncanonical_shape(path: str) -> None:
    assert not normalized_root(path)


@pytest.mark.parametrize("path", ["leaf", "nested/leaf", "snowman-☃"])
def test_normalized_relative_path_accepts_canonical_descendants(path: str) -> None:
    assert normalized_relative_path(path)


@pytest.mark.parametrize("path", ["", "/leaf", "leaf/", "leaf//child", ".", "..", "a/./b", "a/../b", "nul\0"])
def test_normalized_relative_path_rejects_every_noncanonical_shape(path: str) -> None:
    assert not normalized_relative_path(path)


def test_linux_root_walk_uses_path_descriptors_for_execute_only_directories(tmp_path: Path) -> None:
    root = tmp_path / "execute-only"
    nested = root / "nested"
    nested.mkdir(parents=True)
    leaf = nested / "leaf"
    leaf.write_bytes(b"content")
    root.chmod(0o111)
    nested.chmod(0o111)
    descriptor: int | None = None
    try:
        descriptor = open_linux_root(str(nested))
        assert descriptor is not None
        assert os.fstat(descriptor).st_ino == nested.stat().st_ino
        assert os.stat("leaf", dir_fd=descriptor, follow_symlinks=False).st_ino == leaf.stat().st_ino
    finally:
        if descriptor is not None:
            os.close(descriptor)
        root.chmod(0o700)
        nested.chmod(0o700)


def test_linux_root_walk_reports_absence_and_refuses_symlink_ancestors(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)

    assert open_linux_root(str(tmp_path / "missing" / "nested")) is None
    with pytest.raises(ConfinedOpenError) as raised:
        open_linux_root(str(link / "nested"))
    assert raised.value.kind is ConfinedOpenFailure.UNSUPPORTED_OBJECT


def test_linux_root_walk_refuses_an_ancestor_without_search_permission(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    child = blocked / "child"
    child.mkdir(parents=True)
    blocked.chmod(0)
    try:
        with pytest.raises(ConfinedOpenError) as raised:
            open_linux_root(str(child))
    finally:
        blocked.chmod(0o700)
    assert raised.value.kind is ConfinedOpenFailure.IO


def test_linux_root_walk_closes_every_owned_descriptor_after_mid_walk_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = iter((101, 102))
    closed: list[int] = []

    def failing_open(path: str, flags: int, *, dir_fd: int | None = None) -> int:
        del flags, dir_fd
        if path == "second":
            raise OSError(errno.EIO, "injected")
        return next(opened)

    monkeypatch.setattr(os, "open", failing_open)
    monkeypatch.setattr(os, "close", closed.append)

    with pytest.raises(ConfinedOpenError) as raised:
        open_linux_root("/first/second")

    assert raised.value.kind is ConfinedOpenFailure.IO
    assert closed == [101, 102]


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
