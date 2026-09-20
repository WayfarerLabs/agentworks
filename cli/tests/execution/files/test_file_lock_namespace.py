"""Read-only traversal of the fixed machine lock namespace."""

from __future__ import annotations

import errno
import os
import sys
import time
from pathlib import Path

import pytest

import agentworks.execution._file_lock as lock_module
from agentworks.execution._file_lock import FileLockError, FileLockFailureKind

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux lock namespace")

_COMPONENTS = ("var", "lib", "agentworks", "execution")


def _namespace(root: Path) -> tuple[list[Path], Path]:
    directories = [root]
    root.chmod(0o700)
    for component in _COMPONENTS:
        child = directories[-1] / component
        child.mkdir(mode=0o700)
        directories.append(child)
    lock = directories[-1] / "files.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o444)
    return directories, lock


def _root_fd(root: Path) -> int:
    return os.open(root, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)


def test_existing_namespace_locks_without_directory_read_or_mutation(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("directory read authority must be tested without root bypass")
    import fcntl

    directories, lock = _namespace(tmp_path)
    for path in directories:
        path.chmod(0o111)
    before = [(path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_mode) for path in [*directories, lock]]
    root_fd = _root_fd(tmp_path)
    contender = os.open(lock, os.O_RDONLY)
    try:
        with (
            lock_module._file_lock_at_root(root_fd, os.getuid(), expires_at=time.monotonic() + 1),
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        after = [(path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_mode) for path in [*directories, lock]]
        assert after == before
        assert os.fstat(root_fd).st_ino == tmp_path.stat().st_ino
    finally:
        os.close(contender)
        os.close(root_fd)
        for path in directories:
            path.chmod(0o700)


@pytest.mark.parametrize("index", range(5))
def test_writable_namespace_ancestor_refuses_before_locking(tmp_path: Path, index: int) -> None:
    directories, lock = _namespace(tmp_path)
    directories[index].chmod(0o722)
    root_fd = _root_fd(tmp_path)
    try:
        with (
            pytest.raises(FileLockError) as caught,
            lock_module._file_lock_at_root(root_fd, os.getuid(), expires_at=None),
        ):
            pytest.fail("unsafe ancestor admitted")
        assert caught.value.kind is FileLockFailureKind.UNSAFE
        assert lock.stat().st_mode & 0o777 == 0o444
        assert os.fstat(root_fd).st_ino == tmp_path.stat().st_ino
    finally:
        os.close(root_fd)


@pytest.mark.parametrize("index", range(1, 5))
def test_linked_namespace_ancestor_is_not_followed(tmp_path: Path, index: int) -> None:
    directories, _ = _namespace(tmp_path)
    path = directories[index]
    retained = tmp_path / "retained"
    path.rename(retained)
    path.symlink_to(retained, target_is_directory=True)
    root_fd = _root_fd(tmp_path)
    try:
        with (
            pytest.raises(FileLockError) as caught,
            lock_module._file_lock_at_root(root_fd, os.getuid(), expires_at=None),
        ):
            pytest.fail("symlink ancestor admitted")
        assert caught.value.kind is FileLockFailureKind.UNSAFE
        assert path.is_symlink()
    finally:
        os.close(root_fd)


@pytest.mark.parametrize("missing", ["ancestor", "lock"])
def test_missing_state_is_not_installed(tmp_path: Path, missing: str) -> None:
    directories, lock = _namespace(tmp_path)
    removed = directories[-1] if missing == "ancestor" else lock
    retained = tmp_path / "retained"
    removed.rename(retained)
    root_fd = _root_fd(tmp_path)
    try:
        with (
            pytest.raises(FileLockError) as caught,
            lock_module._file_lock_at_root(root_fd, os.getuid(), expires_at=None),
        ):
            pytest.fail("missing lock state admitted")
        assert caught.value.kind is FileLockFailureKind.MISSING
        assert not removed.exists()
        assert retained.exists()
    finally:
        os.close(root_fd)


def test_wrong_namespace_owner_refuses(tmp_path: Path) -> None:
    _namespace(tmp_path)
    root_fd = _root_fd(tmp_path)
    try:
        with (
            pytest.raises(FileLockError) as caught,
            lock_module._file_lock_at_root(root_fd, os.getuid() + 1, expires_at=None),
        ):
            pytest.fail("wrong namespace owner admitted")
        assert caught.value.kind is FileLockFailureKind.UNSAFE
    finally:
        os.close(root_fd)


@pytest.mark.parametrize("failure", [OSError(errno.EIO, "private input"), KeyboardInterrupt()])
def test_post_open_observation_failure_closes_owned_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    _namespace(tmp_path)
    root_fd = _root_fd(tmp_path)
    real_open = lock_module._open_namespace_directory
    real_fstat = os.fstat
    opened: list[int] = []

    def track_open(name: str, parent_fd: int | None) -> int:
        descriptor = real_open(name, parent_fd)
        opened.append(descriptor)
        return descriptor

    def fail_observation(descriptor: int) -> os.stat_result:
        if len(opened) == 2 and descriptor == opened[-1]:
            raise failure
        return real_fstat(descriptor)

    monkeypatch.setattr(lock_module, "_open_namespace_directory", track_open)
    monkeypatch.setattr(os, "fstat", fail_observation)
    try:
        with (
            pytest.raises(FileLockError if isinstance(failure, OSError) else KeyboardInterrupt) as caught,
            lock_module._file_lock_at_root(root_fd, os.getuid(), expires_at=None),
        ):
            pytest.fail("observation fault ignored")
        if isinstance(caught.value, FileLockError):
            assert caught.value.kind is FileLockFailureKind.IO
            assert caught.value.args == ("io",)
        assert len(opened) == 2
        for descriptor in opened:
            with pytest.raises(OSError) as closed:
                real_fstat(descriptor)
            assert closed.value.errno == errno.EBADF
        assert real_fstat(root_fd).st_ino == tmp_path.stat().st_ino
    finally:
        os.close(root_fd)


def test_namespace_replacement_after_open_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _namespace(tmp_path)
    root_fd = _root_fd(tmp_path)
    real_open = lock_module._open_namespace_directory

    def replace_after_open(name: str, parent_fd: int | None) -> int:
        descriptor = real_open(name, parent_fd)
        if name == "var":
            (tmp_path / "var").rename(tmp_path / "retained")
            (tmp_path / "var").mkdir(mode=0o700)
        return descriptor

    monkeypatch.setattr(lock_module, "_open_namespace_directory", replace_after_open)
    try:
        with (
            pytest.raises(FileLockError) as caught,
            lock_module._file_lock_at_root(root_fd, os.getuid(), expires_at=None),
        ):
            pytest.fail("replacement ignored")
        assert caught.value.kind is FileLockFailureKind.CONFLICT
    finally:
        os.close(root_fd)


def test_expired_system_lock_does_not_open_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_open(name: str, parent_fd: int | None) -> int:
        pytest.fail("expired operation opened the namespace")

    monkeypatch.setattr(lock_module, "_open_namespace_directory", unexpected_open)
    with pytest.raises(FileLockError) as caught, lock_module.system_file_lock(expires_at=0.0):
        pytest.fail("expired operation acquired the lock")
    assert caught.value.kind is FileLockFailureKind.DEADLINE
