"""Owned local trust files, publication, and shared admission and exclusive maintenance locks.

Parents must be operator-controlled. These checks reject links and unsafe owned
modes; they do not defend against hostile code running as the same local user.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import BinaryIO


MANIFEST_LIMIT = 1024 * 1024


class TrustBusyError(StateError):
    """Another process owns this bundle's conflicting maintenance or admission lock."""


def check_path(path: Path, *, directory: bool = False, owned: bool = False) -> None:
    """Inspect an explicit local path without following symlinks or reparse points."""
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise StateError("SSH trust paths cannot contain links or reparse points")
        expected_directory = component != path or directory
        if not (stat.S_ISDIR(info.st_mode) if expected_directory else stat.S_ISREG(info.st_mode)):
            raise StateError("SSH trust requires regular files beneath real directories")
        if (
            owned
            and component == path
            and os.name != "nt"
            and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077)
        ):
            raise StateError("Managed SSH trust must be owned by this user with private permissions")


@contextmanager
def read_file(path: Path, *, owned: bool = False) -> Iterator[BinaryIO]:
    check_path(path, owned=owned)
    if sys.platform == "win32":
        # This retained leaf has no execution-stack dependencies. Its handle
        # denies writes and replacement while the source is being captured.
        from agentworks._package_source_windows import locked_file

        with locked_file(path) as descriptor, os.fdopen(os.dup(descriptor), "rb") as source:
            yield source
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise StateError("SSH trust requires regular files")
            yield source


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _snapshot_chunks(source: BinaryIO, remaining: int) -> Iterator[bytes]:
    """An appending writer cannot extend this read beyond the initial file size."""
    while remaining:
        chunk = source.read(min(remaining, 256 * 1024))
        if not chunk:
            raise StateError("SSH trust file became shorter while reading; supply a stable snapshot")
        remaining -= len(chunk)
        yield chunk


def _verify_snapshot(source: BinaryIO, path: Path, before: os.stat_result) -> None:
    current = path.lstat()
    # CPython on Windows reports change time from fstat, but preserves creation
    # time in pathname stat's ctime. Keep the descriptor change-time comparison;
    # only compare fields with matching meanings across descriptor/path queries.
    same_path = (
        os.path.samestat(before, current)
        and before.st_size == current.st_size
        and before.st_mtime_ns == current.st_mtime_ns
        and (sys.platform == "win32" or before.st_ctime_ns == current.st_ctime_ns)
    )
    if _identity(before) != _identity(os.fstat(source.fileno())) or not same_path:
        raise StateError("SSH trust file changed while reading; supply a stable snapshot")


def copy_file(source_path: Path, destination: Path) -> str:
    """Copy complete bytes from a caller-owned stable snapshot, refusing observed change."""
    digest = hashlib.sha256()
    with read_file(source_path) as source:
        before = os.fstat(source.fileno())
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as target:
            for chunk in _snapshot_chunks(source, before.st_size):
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        _verify_snapshot(source, source_path, before)
    return digest.hexdigest()


def file_hash(path: Path, *, owned: bool = True) -> str:
    digest = hashlib.sha256()
    with read_file(path, owned=owned) as source:
        before = os.fstat(source.fileno())
        for chunk in _snapshot_chunks(source, before.st_size):
            digest.update(chunk)
        _verify_snapshot(source, path, before)
    return digest.hexdigest()


def sync_directory(path: Path) -> None:
    """Flush directory publication on POSIX; Windows durability needs native evidence."""
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_state(directory: Path, state: dict[str, object]) -> None:
    """Publish a flushed manifest without a remove-then-rename fallback."""
    payload = json.dumps(state, sort_keys=True).encode("utf-8") + b"\n"
    if len(payload) > MANIFEST_LIMIT:
        raise StateError("SSH trust manifest exceeds the supported size")
    check_path(directory, directory=True, owned=True)
    destination = directory / "state.json"
    if destination.exists() or destination.is_symlink():
        check_path(destination, owned=True)
    temporary = directory / f".state-{uuid.uuid4().hex}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
        sync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def create_bundle(directory: Path) -> None:
    check_path(directory.parent, directory=True)
    directory.mkdir(mode=0o700)
    descriptor = os.open(directory / "lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as lock:
        lock.write(b"\0")
        lock.flush()
        os.fsync(lock.fileno())
    sync_directory(directory)
    sync_directory(directory.parent)


def _windows_lock(descriptor: int, *, shared: bool) -> None:
    """Lock the permanent one-byte range, with independent handles for each caller."""
    if sys.platform != "win32":
        raise OSError("Windows trust locks are unavailable")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("internal", ctypes.c_size_t),
            ("internal_high", ctypes.c_size_t),
            ("offset", wintypes.DWORD),
            ("offset_high", wintypes.DWORD),
            ("event", wintypes.HANDLE),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    arguments = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    kernel.LockFileEx.argtypes = arguments
    kernel.LockFileEx.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(descriptor)
    offset = Overlapped()
    # FAIL_IMMEDIATELY, plus EXCLUSIVE_LOCK only for maintenance. CRT read-lock
    # constants are exclusive too, so they cannot implement concurrent admission.
    flags = 1 if shared else 3
    if not kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(offset)):
        error = ctypes.get_last_error()
        if error == 33:  # ERROR_LOCK_VIOLATION.
            raise TrustBusyError("Another operation holds a conflicting SSH trust lock")
        raise ctypes.WinError(error)


class BundleLock:
    """Passive owner assigned before acquisition; uncertain native ownership stays visible."""

    def __init__(self, directory: Path, *, shared: bool = False) -> None:
        self._directory = directory
        self._shared = shared
        self._descriptor: int | None = None
        self._open_unproven = False
        self._close_unproven = False

    @property
    def settled(self) -> bool:
        """No retained descriptor or uncertain interrupted native acquisition/release."""
        return self._descriptor is None and not self._open_unproven and not self._close_unproven

    def acquire(self) -> None:
        """Acquire once; a failed or interrupted attempt must be released by this owner."""
        if not self.settled:
            raise StateError("SSH trust lock ownership must settle before another acquisition")
        check_path(self._directory, directory=True, owned=True)
        path = self._directory / "lock"
        check_path(path, owned=True)
        # Interrupting open before Python captures the descriptor cannot prove
        # settlement. Once captured, even interrupted lock acquisition can be
        # settled by closing that exact retained descriptor.
        self._open_unproven = True
        try:
            self._descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        except OSError:
            self._open_unproven = False
            raise
        self._open_unproven = False
        descriptor = self._descriptor
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != 1:
            raise StateError("SSH trust lock is not a regular initialized lock file")
        if sys.platform == "win32":
            _windows_lock(descriptor, shared=self._shared)
        else:
            import fcntl

            try:
                fcntl.flock(descriptor, (fcntl.LOCK_SH if self._shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                raise TrustBusyError("Another operation holds a conflicting SSH trust lock") from error

    def release(self) -> bool:
        """Close the exact descriptor, releasing even an unconfirmed acquired lock."""
        if self._close_unproven:
            # Close may already have succeeded and the number may be reused.
            # Retrying it would risk closing an unrelated caller's descriptor.
            return False
        if self._descriptor is None:
            return self.settled
        self._close_unproven = True
        try:
            os.close(self._descriptor)
        except OSError:
            return False
        self._descriptor = None
        self._open_unproven = False
        self._close_unproven = False
        return True


@contextmanager
def bundle_lock(directory: Path, *, shared: bool = False) -> Iterator[None]:
    """Keep one permanent lock file; readers overlap, publication excludes readers."""
    owner = BundleLock(directory, shared=shared)
    failure: BaseException | None = None
    try:
        owner.acquire()
        yield
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            settled = owner.release()
        except BaseException as release_error:
            release_error.add_note("SSH trust lock ownership is unproven; quiesce new use and inspect ownership")
            if failure is None:
                raise
            if not isinstance(failure, (KeyboardInterrupt, SystemExit)):
                raise release_error from failure
            failure.add_note(f"SSH trust lock release was interrupted: {release_error}")
        else:
            if not settled:
                if failure is None:
                    raise StateError("SSH trust lock release is unproven; quiesce new use and inspect ownership")
                failure.add_note("SSH trust lock ownership is unproven; quiesce new use and inspect ownership")
