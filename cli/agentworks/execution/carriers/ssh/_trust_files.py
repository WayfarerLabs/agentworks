"""Owned local trust files, publication, and short admission locks.

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
    """Another process owns this bundle's publication or admission lock."""


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
    if _identity(before) != _identity(os.fstat(source.fileno())) or _identity(before) != _identity(path.lstat()):
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


@contextmanager
def bundle_lock(directory: Path) -> Iterator[None]:
    """Never unlink the lock: all processes must keep locking the same file."""
    check_path(directory, directory=True, owned=True)
    path = directory / "lock"
    check_path(path, owned=True)
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode) or os.fstat(descriptor).st_size != 1:
            raise StateError("SSH trust lock is not a regular initialized lock file")
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise TrustBusyError(
                "Another operation is maintaining or admitting SSH trust; retry when it finishes"
            ) from error
        try:
            yield
        finally:
            if sys.platform == "win32":
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
