"""Debian fixed lock-namespace provisioning behavior."""

from __future__ import annotations

import errno
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import agentworks.execution._file_lock_setup as lock_setup_module
from agentworks.capabilities.vm_platform.bootstrap_script import _FILE_LOCK_SETUP_COMMAND, generate_bootstrap_script
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.execution._file_lock import file_lock
from agentworks.execution._file_lock_setup import (
    FileLockSetupError,
    FileLockSetupFailureKind,
    FileLockSetupObject,
    FileLockSetupPhase,
    main,
    setup_file_lock_namespace,
)
from agentworks.execution._file_lock_setup_bundle import FIXED_SOURCE

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux lock setup")

_ACCESS_ACL = "system.posix_acl_access"
_DEFAULT_ACL = "system.posix_acl_default"


def _open_parent(path: Path) -> int:
    path.chmod(0o755)
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def _setup(parent_fd: int, owner_uid: int | None = None):
    return setup_file_lock_namespace(parent_fd, os.getuid() if owner_uid is None else owner_uid)


def _failure(parent_fd: int, owner_uid: int | None = None) -> FileLockSetupError:
    with pytest.raises(FileLockSetupError) as raised:
        _setup(parent_fd, owner_uid)
    return raised.value


def _assert_no_acl(path: Path, attribute: str) -> None:
    with pytest.raises(OSError) as raised:
        os.getxattr(path, attribute)
    assert raised.value.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}


def _setfacl(path: Path, *arguments: str) -> None:
    executable = shutil.which("setfacl")
    if executable is None:
        pytest.skip("setfacl is unavailable")
    completed = subprocess.run([executable, *arguments, str(path)], check=False, capture_output=True)
    if completed.returncode != 0:
        pytest.skip("fixture filesystem does not support POSIX ACLs")


def test_fresh_setup_creates_exact_namespace(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    try:
        result = _setup(parent_fd)
    finally:
        os.close(parent_fd)

    agentworks = tmp_path / "agentworks"
    execution = agentworks / "execution"
    lock = execution / "files.lock"
    assert result.created == (
        FileLockSetupObject.AGENTWORKS_DIRECTORY,
        FileLockSetupObject.EXECUTION_DIRECTORY,
        FileLockSetupObject.LOCK,
    )
    assert _stat_mode(agentworks) == 0o755
    assert _stat_mode(execution) == 0o755
    assert _stat_mode(lock) == 0o444
    assert lock.stat().st_uid == os.getuid()
    assert lock.stat().st_nlink == 1
    assert lock.read_bytes() == b""
    execution_fd = os.open(execution, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with file_lock(execution_fd, os.getuid(), expires_at=None):
            assert os.stat("files.lock", dir_fd=execution_fd, follow_symlinks=False).st_ino == lock.stat().st_ino
    finally:
        os.close(execution_fd)


def test_idempotent_setup_preserves_existing_lock_inode(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    try:
        _setup(parent_fd)
        lock = tmp_path / "agentworks" / "execution" / "files.lock"
        before = lock.stat()
        result = _setup(parent_fd)
        after = lock.stat()
    finally:
        os.close(parent_fd)

    assert result.created == ()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_existing_agentworks_contents_are_untouched(tmp_path: Path) -> None:
    agentworks = tmp_path / "agentworks"
    agentworks.mkdir(mode=0o755)
    agentworks.chmod(0o755)
    unrelated = agentworks / "gce-state"
    unrelated.write_bytes(b"preserve")
    before = unrelated.stat()
    parent_fd = _open_parent(tmp_path)
    try:
        result = _setup(parent_fd)
    finally:
        os.close(parent_fd)

    after = unrelated.stat()
    assert unrelated.read_bytes() == b"preserve"
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert result.created == (FileLockSetupObject.EXECUTION_DIRECTORY, FileLockSetupObject.LOCK)


def test_adverse_umask_does_not_weaken_final_permissions(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    previous = os.umask(0o077)
    try:
        _setup(parent_fd)
    finally:
        os.umask(previous)
        os.close(parent_fd)

    assert _stat_mode(tmp_path / "agentworks") == 0o755
    assert _stat_mode(tmp_path / "agentworks" / "execution") == 0o755
    assert _stat_mode(tmp_path / "agentworks" / "execution" / "files.lock") == 0o444


def test_inherited_directory_acls_are_removed_from_new_objects(tmp_path: Path) -> None:
    _setfacl(tmp_path, "-d", "-m", f"u:{os.getuid() + 1}:---")
    parent_fd = _open_parent(tmp_path)
    try:
        _setup(parent_fd)
    finally:
        os.close(parent_fd)

    agentworks = tmp_path / "agentworks"
    _assert_no_acl(agentworks, _ACCESS_ACL)
    _assert_no_acl(agentworks, _DEFAULT_ACL)
    _assert_no_acl(agentworks / "execution", _ACCESS_ACL)
    _assert_no_acl(agentworks / "execution", _DEFAULT_ACL)


def test_inherited_lock_acl_is_removed_from_new_lock(tmp_path: Path) -> None:
    agentworks = tmp_path / "agentworks"
    execution = agentworks / "execution"
    execution.mkdir(parents=True)
    agentworks.chmod(0o755)
    execution.chmod(0o755)
    _setfacl(execution, "-d", "-m", f"u:{os.getuid() + 1}:---")
    parent_fd = _open_parent(tmp_path)
    try:
        result = _setup(parent_fd)
    finally:
        os.close(parent_fd)

    lock = execution / "files.lock"
    assert result.created == (FileLockSetupObject.LOCK,)
    assert _stat_mode(lock) == 0o444
    _assert_no_acl(lock, _ACCESS_ACL)


def test_existing_access_acl_is_refused_without_rewrite(tmp_path: Path) -> None:
    agentworks = tmp_path / "agentworks"
    agentworks.mkdir(mode=0o755)
    _setfacl(agentworks, "-m", f"u:{os.getuid() + 1}:---")
    acl_before = os.getxattr(agentworks, _ACCESS_ACL)
    parent_fd = _open_parent(tmp_path)
    try:
        error = _failure(parent_fd)
    finally:
        os.close(parent_fd)

    assert error.kind is FileLockSetupFailureKind.UNSAFE
    assert error.phase is FileLockSetupPhase.AGENTWORKS
    assert os.getxattr(agentworks, _ACCESS_ACL) == acl_before


@pytest.mark.parametrize("mode", [0o777, 0o775, 0o750])
def test_unsafe_existing_directory_mode_is_refused(tmp_path: Path, mode: int) -> None:
    agentworks = tmp_path / "agentworks"
    agentworks.mkdir(mode=0o755)
    agentworks.chmod(mode)
    parent_fd = _open_parent(tmp_path)
    try:
        error = _failure(parent_fd)
    finally:
        os.close(parent_fd)
    assert error.kind is FileLockSetupFailureKind.UNSAFE
    assert _stat_mode(agentworks) == mode


def test_owner_mismatch_is_refused(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    try:
        error = _failure(parent_fd, os.getuid() + 1)
    finally:
        os.close(parent_fd)
    assert error.kind is FileLockSetupFailureKind.UNSAFE
    assert error.phase is FileLockSetupPhase.LIB


def test_symlink_namespace_component_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "agentworks").symlink_to(target, target_is_directory=True)
    parent_fd = _open_parent(tmp_path)
    try:
        error = _failure(parent_fd)
    finally:
        os.close(parent_fd)
    assert error.kind is FileLockSetupFailureKind.UNSAFE
    assert not (target / "execution").exists()


def test_existing_hard_linked_lock_is_refused(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    try:
        _setup(parent_fd)
        lock = tmp_path / "agentworks" / "execution" / "files.lock"
        os.link(lock, tmp_path / "second-name")
        error = _failure(parent_fd)
    finally:
        os.close(parent_fd)
    assert error.kind is FileLockSetupFailureKind.UNSAFE


def test_existing_lock_mode_is_refused_without_repair(tmp_path: Path) -> None:
    parent_fd = _open_parent(tmp_path)
    try:
        _setup(parent_fd)
        lock = tmp_path / "agentworks" / "execution" / "files.lock"
        lock.chmod(0o644)
        inode = lock.stat().st_ino
        error = _failure(parent_fd)
    finally:
        os.close(parent_fd)
    assert error.kind is FileLockSetupFailureKind.UNSAFE
    assert lock.stat().st_ino == inode
    assert _stat_mode(lock) == 0o644


@pytest.mark.parametrize("kind", ["directory", "fifo", "socket", "symlink"])
def test_special_lock_is_refused(kind: str) -> None:
    with tempfile.TemporaryDirectory(prefix="agw-lock-", dir="/tmp") as temporary:
        root = Path(temporary)
        execution = root / "agentworks" / "execution"
        execution.mkdir(parents=True)
        root.chmod(0o755)
        execution.parent.chmod(0o755)
        execution.chmod(0o755)
        target = execution / "files.lock"
        listener: socket.socket | None = None
        if kind == "directory":
            target.mkdir()
        elif kind == "fifo":
            os.mkfifo(target, 0o444)
        elif kind == "socket":
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(target))
        else:
            destination = execution / "target"
            destination.touch()
            target.symlink_to(destination)
        parent_fd = _open_parent(root)
        try:
            error = _failure(parent_fd)
        finally:
            os.close(parent_fd)
            if listener is not None:
                listener.close()
        assert error.kind is FileLockSetupFailureKind.UNSAFE


def test_partial_failure_reports_created_objects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _open_parent(tmp_path)
    original_open = os.open

    def failing_open(path: str | bytes | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if path == "files.lock":
            raise OSError(errno.EIO, "fixture")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", failing_open)
    try:
        error = _failure(parent_fd)
    finally:
        os.close(parent_fd)

    assert error.kind is FileLockSetupFailureKind.IO
    assert error.phase is FileLockSetupPhase.LOCK
    assert error.created == (
        FileLockSetupObject.AGENTWORKS_DIRECTORY,
        FileLockSetupObject.EXECUTION_DIRECTORY,
    )
    assert error.__context__ is None
    assert error.__cause__ is None
    assert (tmp_path / "agentworks" / "execution").is_dir()
    assert not (tmp_path / "agentworks" / "execution" / "files.lock").exists()


def test_main_requires_root_before_fixed_path_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1)
    monkeypatch.setattr(lock_setup_module, "_open_root", lambda _created: pytest.fail("root path was opened"))
    assert main() == 1


def test_bootstrap_uses_fixed_isolated_python_bundle() -> None:
    script = generate_bootstrap_script(
        admin_username="testuser",
        ssh_public_key="",
        provisioning_packages=PROVISIONING_PACKAGES,
        tailscale_auth_key=None,
        hostname="host",
        swap=0,
    )
    assert shlex.split(_FILE_LOCK_SETUP_COMMAND) == ["/usr/bin/python3", "-I", "-S", "-B", "-c", FIXED_SOURCE]
    assert script.count(_FILE_LOCK_SETUP_COMMAND) == 1
    assert (
        script.index("apt-get install")
        < script.index(_FILE_LOCK_SETUP_COMMAND)
        < script.index("mkdir -p /etc/cloud/cloud.cfg.d")
    )


@pytest.mark.parametrize("interpreter", [Path(sys.executable), Path("/usr/bin/python3.11")])
def test_fixed_bundle_executes_without_installed_package(interpreter: Path) -> None:
    if not interpreter.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {interpreter}")
    source = "import os;os.geteuid=lambda:1\n" + FIXED_SOURCE
    completed = subprocess.run(
        [str(interpreter), "-I", "-S", "-B", "-c", source],
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b""


def _stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o7777
