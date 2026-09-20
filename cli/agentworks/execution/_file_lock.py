"""Linux cooperative file transactions using a pre-provisioned fixed lock.

The low-level entry borrows a protected parent descriptor; the system entry
validates the fixed root-owned namespace. Neither creates or repairs lock state.
Provisioning owns setup and the local-filesystem locking prerequisite.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import time
from contextlib import ExitStack, contextmanager, suppress
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_LOCK_NAME = "files.lock"
_LOCK_DIRECTORY = ("var", "lib", "agentworks", "execution")
_POLL_SECONDS = 0.05


class FileLockFailureKind(Enum):
    """Closed lock failures that reveal no filesystem names or metadata."""

    UNSUPPORTED = "unsupported"
    MISSING = "missing"
    UNSAFE = "unsafe"
    CONFLICT = "conflict"
    DEADLINE = "deadline"
    IO = "io"


class FileLockError(Exception):
    """A lock refusal carrying only its closed failure kind."""

    def __init__(self, kind: FileLockFailureKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


@contextmanager
def system_file_lock(*, expires_at: float | None) -> Iterator[None]:
    """Open the fixed root-owned Linux namespace and hold its existing lock.

    Provisioning must establish local-filesystem locking semantics. This path
    validates protected ancestors without creating, repairing or staging files.
    """
    if sys.platform != "linux" or not hasattr(os, "O_PATH"):
        raise FileLockError(FileLockFailureKind.UNSUPPORTED)
    _raise_if_expired(expires_at)
    root_fd = _open_namespace_directory("/", None)
    try:
        with _file_lock_at_root(root_fd, 0, expires_at=expires_at):
            yield
    finally:
        _close(root_fd)


@contextmanager
def _file_lock_at_root(
    root_fd: int,
    trusted_owner_uid: int,
    *,
    expires_at: float | None,
) -> Iterator[None]:
    """Borrow a namespace root; fixtures use the same fixed walk as production."""
    _raise_if_expired(expires_at)
    if not _safe_parent(_fstat(root_fd), trusted_owner_uid):
        raise FileLockError(FileLockFailureKind.UNSAFE)
    with ExitStack() as opened:
        parent_fd = root_fd
        for component in _LOCK_DIRECTORY:
            _raise_if_expired(expires_at)
            descriptor = _open_namespace_directory(component, parent_fd)
            opened.callback(_close, descriptor)
            held = _fstat(descriptor)
            if not _safe_parent(held, trusted_owner_uid):
                raise FileLockError(FileLockFailureKind.UNSAFE)
            named = _stat_namespace_directory(parent_fd, component)
            if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                raise FileLockError(FileLockFailureKind.CONFLICT)
            if not _safe_parent(named, trusted_owner_uid):
                raise FileLockError(FileLockFailureKind.UNSAFE)
            parent_fd = descriptor
        with file_lock(parent_fd, trusted_owner_uid, expires_at=expires_at):
            yield


def _open_namespace_directory(name: str, parent_fd: int | None) -> int:
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    error_number: int | None = None
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        error_number = error.errno
    if error_number == errno.ENOENT:
        raise FileLockError(FileLockFailureKind.MISSING)
    if error_number in {errno.ELOOP, errno.ENOTDIR}:
        raise FileLockError(FileLockFailureKind.UNSAFE)
    raise FileLockError(FileLockFailureKind.IO)


def _stat_namespace_directory(parent_fd: int, name: str) -> os.stat_result:
    error_number: int | None = None
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        error_number = error.errno
    if error_number == errno.ENOENT:
        raise FileLockError(FileLockFailureKind.CONFLICT)
    raise FileLockError(FileLockFailureKind.IO)


@contextmanager
def file_lock(
    parent_fd: int,
    trusted_owner_uid: int,
    *,
    expires_at: float | None,
) -> Iterator[None]:
    """Hold the fixed cooperative lock beneath a borrowed trusted parent.

    ``expires_at`` is a finite guest-monotonic timestamp. ``None`` permits an
    unbounded wait, while every retry remains nonblocking. The caller retains
    ownership of ``parent_fd`` and must establish the protected namespace and
    local-filesystem prerequisites before calling.
    """
    if sys.platform != "linux":
        raise FileLockError(FileLockFailureKind.UNSUPPORTED)

    _raise_if_expired(expires_at)
    parent = _fstat(parent_fd)
    if not _safe_parent(parent, trusted_owner_uid):
        raise FileLockError(FileLockFailureKind.UNSAFE)
    _raise_if_expired(expires_at)

    named_before = _stat_named_lock(parent_fd)
    if named_before is None:
        raise FileLockError(FileLockFailureKind.MISSING)
    if not _safe_leaf(named_before, trusted_owner_uid, parent.st_dev):
        raise FileLockError(FileLockFailureKind.UNSAFE)
    _raise_if_expired(expires_at)
    descriptor = _open_lock(parent_fd)
    try:
        opened = _fstat(descriptor)
        if not _safe_leaf(opened, trusted_owner_uid, parent.st_dev):
            raise FileLockError(FileLockFailureKind.UNSAFE)
        if (opened.st_dev, opened.st_ino) != (named_before.st_dev, named_before.st_ino):
            raise FileLockError(FileLockFailureKind.CONFLICT)

        _acquire(descriptor, expires_at)
        held_parent = _fstat(parent_fd)
        held = _fstat(descriptor)
        named = _stat_named_lock(parent_fd)
        if not _safe_parent(held_parent, trusted_owner_uid):
            raise FileLockError(FileLockFailureKind.UNSAFE)
        if not _safe_leaf(held, trusted_owner_uid, parent.st_dev):
            raise FileLockError(FileLockFailureKind.UNSAFE)
        if named is None or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino):
            raise FileLockError(FileLockFailureKind.CONFLICT)
        if not _safe_leaf(named, trusted_owner_uid, parent.st_dev):
            raise FileLockError(FileLockFailureKind.UNSAFE)
        _raise_if_expired(expires_at)
        yield
    finally:
        _close(descriptor)


def _safe_parent(observed: os.stat_result, trusted_owner_uid: int) -> bool:
    return (
        stat.S_ISDIR(observed.st_mode)
        and observed.st_uid == trusted_owner_uid
        and stat.S_IMODE(observed.st_mode) & 0o022 == 0
    )


def _safe_leaf(observed: os.stat_result, trusted_owner_uid: int, parent_device: int) -> bool:
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_nlink == 1
        and observed.st_uid == trusted_owner_uid
        and stat.S_IMODE(observed.st_mode) == 0o444
        and observed.st_size == 0
        and observed.st_dev == parent_device
    )


def _open_lock(parent_fd: int) -> int:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    error_number: int | None = None
    try:
        descriptor = os.open(_LOCK_NAME, flags, dir_fd=parent_fd)
    except OSError as error:
        error_number = error.errno
    if descriptor is not None:
        return descriptor
    if error_number == errno.ENOENT:
        raise FileLockError(FileLockFailureKind.CONFLICT)
    if error_number in {errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.ENODEV}:
        raise FileLockError(FileLockFailureKind.UNSAFE)
    raise FileLockError(FileLockFailureKind.IO)


def _acquire(descriptor: int, expires_at: float | None) -> None:
    while True:
        _raise_if_expired(expires_at)
        if _flock_nonblocking(descriptor):
            return
        _sleep_before_retry(expires_at)


def _flock_nonblocking(descriptor: int) -> bool:
    try:
        import fcntl
    except ImportError:
        import_failed = True
    else:
        import_failed = False
    if import_failed:
        raise FileLockError(FileLockFailureKind.UNSUPPORTED)

    error_number: int | None = None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        error_number = error.errno
    if error_number is None:
        return True
    if error_number in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
        return False
    raise FileLockError(FileLockFailureKind.IO)


def _stat_named_lock(parent_fd: int) -> os.stat_result | None:
    observed: os.stat_result | None = None
    error_number: int | None = None
    try:
        observed = os.stat(_LOCK_NAME, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        error_number = error.errno
    if observed is not None:
        return observed
    if error_number == errno.ENOENT:
        return None
    raise FileLockError(FileLockFailureKind.IO)


def _fstat(descriptor: int) -> os.stat_result:
    observed: os.stat_result | None = None
    with suppress(OSError):
        observed = os.fstat(descriptor)
    if observed is None:
        raise FileLockError(FileLockFailureKind.IO)
    return observed


def _raise_if_expired(expires_at: float | None) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise FileLockError(FileLockFailureKind.DEADLINE)


def _sleep_before_retry(expires_at: float | None) -> None:
    delay = _POLL_SECONDS
    if expires_at is not None:
        delay = min(delay, max(0.0, expires_at - time.monotonic()))
    if delay > 0:
        time.sleep(delay)


def _close(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)
