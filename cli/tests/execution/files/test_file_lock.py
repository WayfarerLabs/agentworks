"""Linux fixed-lock safety and contention behavior."""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import agentworks.execution._file_lock as lock_module
from agentworks.execution._file_lock import FileLockError, FileLockFailureKind, file_lock

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux file lock")


def _provision_lock(parent: Path) -> int:
    parent.chmod(0o700)
    lock_path = parent / "files.lock"
    lock_path.touch()
    lock_path.chmod(0o444)
    return os.open(parent, os.O_RDONLY | os.O_DIRECTORY)


def _failure(parent_fd: int, *, owner_uid: int | None = None, expires_at: float | None = None) -> FileLockError:
    with (
        pytest.raises(FileLockError) as raised,
        file_lock(parent_fd, os.getuid() if owner_uid is None else owner_uid, expires_at=expires_at),
    ):
        pytest.fail("unsafe lock was acquired")
    return raised.value


def test_lock_is_exclusive_and_release_follows_normal_exit(tmp_path: Path) -> None:
    import fcntl

    parent_fd = _provision_lock(tmp_path)
    contender = os.open(tmp_path / "files.lock", os.O_RDONLY)
    try:
        with file_lock(parent_fd, os.getuid(), expires_at=time.monotonic() + 1.0), pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(contender)
        os.close(parent_fd)


def test_exceptional_exit_releases_lock(tmp_path: Path) -> None:
    import fcntl

    parent_fd = _provision_lock(tmp_path)
    contender = os.open(tmp_path / "files.lock", os.O_RDONLY)
    try:
        with (
            pytest.raises(RuntimeError, match="fixture"),
            file_lock(parent_fd, os.getuid(), expires_at=time.monotonic() + 1.0),
        ):
            raise RuntimeError("fixture")
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(contender)
        os.close(parent_fd)


def test_real_subprocess_contention_obeys_deadline(tmp_path: Path) -> None:
    parent_fd = _provision_lock(tmp_path)
    script = (
        "import fcntl, os, sys; "
        "fd = os.open(sys.argv[1], os.O_RDONLY); "
        "fcntl.flock(fd, fcntl.LOCK_EX); "
        "print('ready', flush=True); "
        "sys.stdin.buffer.read(1)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path / "files.lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None and process.stdout.readline() == "ready\n"
        started = time.monotonic()
        error = _failure(parent_fd, expires_at=started + 0.12)
        assert error.kind is FileLockFailureKind.DEADLINE
        assert time.monotonic() - started < 1.0
    finally:
        if process.stdin is not None:
            process.stdin.write("x")
            process.stdin.close()
        process.wait(timeout=5)
        os.close(parent_fd)


def test_missing_lock_is_closed_refusal_and_is_not_created(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        error = _failure(parent_fd)
    finally:
        os.close(parent_fd)
    assert error.kind is FileLockFailureKind.MISSING
    assert error.args == ("missing",)
    assert not (tmp_path / "files.lock").exists()


@pytest.mark.parametrize("mode", [0o722, 0o720, 0o702])
def test_group_or_other_writable_parent_is_refused(tmp_path: Path, mode: int) -> None:
    parent_fd = _provision_lock(tmp_path)
    tmp_path.chmod(mode)
    try:
        assert _failure(parent_fd).kind is FileLockFailureKind.UNSAFE
    finally:
        os.close(parent_fd)


def test_parent_owner_must_match_trusted_uid(tmp_path: Path) -> None:
    parent_fd = _provision_lock(tmp_path)
    try:
        assert _failure(parent_fd, owner_uid=os.getuid() + 1).kind is FileLockFailureKind.UNSAFE
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o555, 0o2444])
def test_leaf_requires_exact_read_only_mode(tmp_path: Path, mode: int) -> None:
    parent_fd = _provision_lock(tmp_path)
    (tmp_path / "files.lock").chmod(mode)
    try:
        assert _failure(parent_fd).kind is FileLockFailureKind.UNSAFE
    finally:
        os.close(parent_fd)


def test_nonempty_leaf_is_refused_without_modification(tmp_path: Path) -> None:
    parent_fd = _provision_lock(tmp_path)
    target = tmp_path / "files.lock"
    target.chmod(0o600)
    target.write_bytes(b"do not truncate")
    target.chmod(0o444)
    before = target.stat()
    try:
        assert _failure(parent_fd).kind is FileLockFailureKind.UNSAFE
    finally:
        os.close(parent_fd)
    after = target.stat()
    assert target.read_bytes() == b"do not truncate"
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)


def test_hard_link_is_refused(tmp_path: Path) -> None:
    parent_fd = _provision_lock(tmp_path)
    os.link(tmp_path / "files.lock", tmp_path / "second-name")
    try:
        assert _failure(parent_fd).kind is FileLockFailureKind.UNSAFE
    finally:
        os.close(parent_fd)


def test_symbolic_link_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.touch()
    target.chmod(0o444)
    (tmp_path / "files.lock").symlink_to(target)
    tmp_path.chmod(0o700)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert _failure(parent_fd).kind is FileLockFailureKind.UNSAFE
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize("kind", ["directory", "fifo", "socket"])
def test_special_leaf_is_refused_before_open(tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "files.lock"
    listener: socket.socket | None = None
    if kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target, 0o444)
    else:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(target))
    tmp_path.chmod(0o700)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(lock_module, "_open_lock", lambda _fd: pytest.fail("special leaf was opened"))
    try:
        assert _failure(parent_fd).kind is FileLockFailureKind.UNSAFE
    finally:
        os.close(parent_fd)
        if listener is not None:
            listener.close()


@pytest.mark.parametrize(("field", "index"), [("owner", 4), ("device", 2)])
def test_leaf_owner_and_device_must_match_trusted_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, index: int
) -> None:
    parent_fd = _provision_lock(tmp_path)
    observed = list((tmp_path / "files.lock").stat())
    observed[index] += 1
    mismatched = os.stat_result(observed)
    monkeypatch.setattr(lock_module, "_stat_named_lock", lambda _fd: mismatched)
    monkeypatch.setattr(lock_module, "_open_lock", lambda _fd: pytest.fail(f"wrong-{field} leaf was opened"))
    try:
        assert _failure(parent_fd).kind is FileLockFailureKind.UNSAFE
    finally:
        os.close(parent_fd)


def test_open_uses_fixed_read_only_nonblocking_nofollow_cloexec_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_fd = _provision_lock(tmp_path)
    original_open = os.open
    calls: list[tuple[str | bytes | Path, int, int | None]] = []

    def recording_open(path: str | bytes | Path, flags: int, *, dir_fd: int | None = None) -> int:
        calls.append((path, flags, dir_fd))
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", recording_open)
    try:
        with file_lock(parent_fd, os.getuid(), expires_at=time.monotonic() + 1.0):
            pass
    finally:
        os.close(parent_fd)

    assert len(calls) == 1
    path, flags, observed_parent = calls[0]
    required = os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    assert path == "files.lock" and observed_parent == parent_fd
    assert flags & required == required
    assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC) == 0


def test_expired_deadline_refuses_before_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _provision_lock(tmp_path)
    monkeypatch.setattr(lock_module, "_open_lock", lambda _fd: pytest.fail("expired lock was opened"))
    try:
        error = _failure(parent_fd, expires_at=time.monotonic() - 1.0)
    finally:
        os.close(parent_fd)
    assert error.kind is FileLockFailureKind.DEADLINE


def test_deadline_is_checked_after_acquisition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _provision_lock(tmp_path)
    observed_times = iter([0.0, 0.0, 0.0, 2.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(observed_times))
    try:
        assert _failure(parent_fd, expires_at=1.0).kind is FileLockFailureKind.DEADLINE
    finally:
        os.close(parent_fd)


def test_polling_uses_bounded_sleeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _provision_lock(tmp_path)
    outcomes = iter([False, False, True])
    sleeps: list[float] = []
    monkeypatch.setattr(lock_module, "_flock_nonblocking", lambda _fd: next(outcomes))
    monkeypatch.setattr(time, "sleep", sleeps.append)
    try:
        with file_lock(parent_fd, os.getuid(), expires_at=time.monotonic() + 1.0):
            pass
    finally:
        os.close(parent_fd)
    assert len(sleeps) == 2
    assert all(0 < delay <= lock_module._POLL_SECONDS for delay in sleeps)


def test_named_replacement_after_acquisition_is_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _provision_lock(tmp_path)
    original_flock = lock_module._flock_nonblocking
    replaced = False

    def replacing_flock(descriptor: int) -> bool:
        nonlocal replaced
        acquired = original_flock(descriptor)
        if not replaced:
            replaced = True
            os.rename(tmp_path / "files.lock", tmp_path / "old-lock")
            (tmp_path / "files.lock").touch()
            (tmp_path / "files.lock").chmod(0o444)
        return acquired

    monkeypatch.setattr(lock_module, "_flock_nonblocking", replacing_flock)
    try:
        assert _failure(parent_fd).kind is FileLockFailureKind.CONFLICT
    finally:
        os.close(parent_fd)


def test_each_transaction_opens_a_fresh_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _provision_lock(tmp_path)
    original_open = lock_module._open_lock
    opens = 0

    def counting_open(descriptor: int) -> int:
        nonlocal opens
        opens += 1
        return original_open(descriptor)

    monkeypatch.setattr(lock_module, "_open_lock", counting_open)
    try:
        for _ in range(2):
            with file_lock(parent_fd, os.getuid(), expires_at=time.monotonic() + 1.0):
                pass
    finally:
        os.close(parent_fd)
    assert opens == 2


def test_descriptor_is_closed_after_pre_yield_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _provision_lock(tmp_path)
    original_open = lock_module._open_lock
    opened: list[int] = []

    def recording_open(descriptor: int) -> int:
        result = original_open(descriptor)
        opened.append(result)
        return result

    def refusing_flock(_descriptor: int) -> bool:
        raise FileLockError(FileLockFailureKind.IO)

    monkeypatch.setattr(lock_module, "_open_lock", recording_open)
    monkeypatch.setattr(lock_module, "_flock_nonblocking", refusing_flock)
    try:
        assert _failure(parent_fd).kind is FileLockFailureKind.IO
    finally:
        os.close(parent_fd)
    assert len(opened) == 1
    with pytest.raises(OSError) as raised:
        os.fstat(opened[0])
    assert raised.value.errno == errno.EBADF


def test_operating_system_error_is_not_chained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent_fd = _provision_lock(tmp_path)

    def failing_open(_path: str, _flags: int, *, dir_fd: int) -> int:
        raise OSError(errno.EIO, "sensitive fixture path")

    monkeypatch.setattr(os, "open", failing_open)
    try:
        with pytest.raises(FileLockError) as raised, file_lock(parent_fd, os.getuid(), expires_at=None):
            pass
    finally:
        os.close(parent_fd)
    assert raised.value.kind is FileLockFailureKind.IO
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


def test_non_linux_refuses_without_parent_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(lock_module, "_fstat", lambda _fd: pytest.fail("parent was inspected"))
    error = _failure(0, owner_uid=0)
    assert error.kind is FileLockFailureKind.UNSUPPORTED


def test_module_import_does_not_eagerly_import_fcntl() -> None:
    script = (
        "import builtins; original = builtins.__import__; "
        "builtins.__import__ = lambda name, *a, **k: "
        "(_ for _ in ()).throw(RuntimeError('fcntl imported')) if name == 'fcntl' else original(name, *a, **k); "
        "import agentworks.execution._file_lock"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[3],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
