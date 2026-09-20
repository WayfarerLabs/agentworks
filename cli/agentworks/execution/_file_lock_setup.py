"""Provision the fixed Debian cooperative-lock namespace.

The fixed entry point is privileged create-time setup. File operations and
readiness checks never call it. Existing objects are validation-only; only
objects created by this invocation may have permissions or ACLs finalized.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import NoReturn

_ACCESS_ACL = "system.posix_acl_access"
_DEFAULT_ACL = "system.posix_acl_default"
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_FLAGS |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
_LOCK_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_LOCK_FLAGS |= getattr(os, "O_NONBLOCK", 0)


class FileLockSetupFailureKind(Enum):
    """Closed setup failures that expose no host filesystem details."""

    UNSUPPORTED = "unsupported"
    IDENTITY = "identity"
    MISSING = "missing"
    UNSAFE = "unsafe"
    CONFLICT = "conflict"
    IO = "io"


class FileLockSetupPhase(Enum):
    """Fixed namespace phase in which setup stopped."""

    ROOT = "root"
    VAR = "var"
    LIB = "lib"
    AGENTWORKS = "agentworks"
    EXECUTION = "execution"
    LOCK = "lock"


class FileLockSetupObject(Enum):
    """Fixed object created during this setup attempt."""

    AGENTWORKS_DIRECTORY = "agentworks_directory"
    EXECUTION_DIRECTORY = "execution_directory"
    LOCK = "lock"


@dataclass(frozen=True)
class FileLockSetupResult:
    """Exact persistent objects created by a successful attempt."""

    created: tuple[FileLockSetupObject, ...]


class FileLockSetupError(Exception):
    """A closed refusal with truthful completed creation facts."""

    def __init__(
        self,
        kind: FileLockSetupFailureKind,
        phase: FileLockSetupPhase,
        *,
        created: tuple[FileLockSetupObject, ...] = (),
    ) -> None:
        self.kind = kind
        self.phase = phase
        self.created = created
        super().__init__(kind.value, phase.value, len(created))


def setup_system_file_lock() -> FileLockSetupResult:
    """Provision ``/var/lib/agentworks/execution/files.lock`` as Linux root."""
    if sys.platform != "linux":
        raise FileLockSetupError(FileLockSetupFailureKind.UNSUPPORTED, FileLockSetupPhase.ROOT)
    if os.geteuid() != 0:
        raise FileLockSetupError(FileLockSetupFailureKind.IDENTITY, FileLockSetupPhase.ROOT)

    created: list[FileLockSetupObject] = []
    root_fd = _open_root(created)
    try:
        _require_directory(root_fd, 0, FileLockSetupPhase.ROOT, created)
        var_fd = _open_existing_directory(root_fd, "var", 0, FileLockSetupPhase.VAR, created)
        try:
            lib_fd = _open_existing_directory(var_fd, "lib", 0, FileLockSetupPhase.LIB, created)
            try:
                return setup_file_lock_namespace(lib_fd, 0)
            finally:
                _close(lib_fd)
        finally:
            _close(var_fd)
    finally:
        _close(root_fd)


def setup_file_lock_namespace(parent_fd: int, trusted_owner_uid: int) -> FileLockSetupResult:
    """Create or validate the fixed namespace beneath a borrowed ``lib`` fd.

    The worker exists so unprivileged local fixtures exercise the same
    descriptor-relative creation and validation used by the fixed root entry.
    """
    created: list[FileLockSetupObject] = []
    _require_directory(parent_fd, trusted_owner_uid, FileLockSetupPhase.LIB, created)
    agentworks_fd = _ensure_directory(
        parent_fd,
        "agentworks",
        trusted_owner_uid,
        FileLockSetupPhase.AGENTWORKS,
        FileLockSetupObject.AGENTWORKS_DIRECTORY,
        created,
    )
    try:
        execution_fd = _ensure_directory(
            agentworks_fd,
            "execution",
            trusted_owner_uid,
            FileLockSetupPhase.EXECUTION,
            FileLockSetupObject.EXECUTION_DIRECTORY,
            created,
        )
        try:
            _ensure_lock(execution_fd, trusted_owner_uid, created)
        finally:
            _close(execution_fd)
    finally:
        _close(agentworks_fd)
    return FileLockSetupResult(tuple(created))


def main() -> int:
    """Run fixed privileged setup with one closed failure diagnostic."""
    try:
        setup_system_file_lock()
    except FileLockSetupError as error:
        diagnostic = {
            "created": [created.value for created in error.created],
            "kind": error.kind.value,
            "phase": error.phase.value,
        }
        with suppress(OSError):
            sys.stderr.write(json.dumps(diagnostic, separators=(",", ":"), sort_keys=True) + "\n")
        return 1
    return 0


def _open_root(created: list[FileLockSetupObject]) -> int:
    descriptor: int | None = None
    with suppress(OSError):
        descriptor = os.open("/", _DIRECTORY_FLAGS)
    if descriptor is None:
        _fail(FileLockSetupFailureKind.IO, FileLockSetupPhase.ROOT, created)
    return descriptor


def _open_existing_directory(
    parent_fd: int,
    name: str,
    trusted_owner_uid: int,
    phase: FileLockSetupPhase,
    created: list[FileLockSetupObject],
    *,
    allow_unfinalized: bool = False,
) -> int:
    named = _stat_at(parent_fd, name, phase, created)
    if named is None:
        _fail(FileLockSetupFailureKind.MISSING, phase, created)
    if not _owned_directory_stat(named, trusted_owner_uid):
        _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
    if not allow_unfinalized and not _safe_directory_stat(named, trusted_owner_uid):
        _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
    descriptor = _open_at(parent_fd, name, _DIRECTORY_FLAGS, phase, created)
    try:
        opened = _fstat(descriptor, phase, created)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            _fail(FileLockSetupFailureKind.CONFLICT, phase, created)
        if allow_unfinalized:
            if not _owned_directory_stat(opened, trusted_owner_uid):
                _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
        else:
            _require_directory(descriptor, trusted_owner_uid, phase, created)
    except BaseException:
        _close(descriptor)
        raise
    return descriptor


def _ensure_directory(
    parent_fd: int,
    name: str,
    trusted_owner_uid: int,
    phase: FileLockSetupPhase,
    setup_object: FileLockSetupObject,
    created: list[FileLockSetupObject],
) -> int:
    created_now = _mkdir_if_missing(parent_fd, name, phase, created)
    if created_now:
        created.append(setup_object)
    descriptor = _open_existing_directory(
        parent_fd,
        name,
        trusted_owner_uid,
        phase,
        created,
        allow_unfinalized=created_now,
    )
    if created_now:
        try:
            _finalize_new_directory(descriptor, trusted_owner_uid, phase, created)
        except BaseException:
            _close(descriptor)
            raise
    return descriptor


def _mkdir_if_missing(
    parent_fd: int,
    name: str,
    phase: FileLockSetupPhase,
    created: list[FileLockSetupObject],
) -> bool:
    error_number: int | None = None
    try:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
    except OSError as error:
        error_number = error.errno
    if error_number is None:
        return True
    if error_number == errno.EEXIST:
        return False
    _fail(FileLockSetupFailureKind.IO, phase, created)


def _finalize_new_directory(
    descriptor: int,
    trusted_owner_uid: int,
    phase: FileLockSetupPhase,
    created: list[FileLockSetupObject],
) -> None:
    observed = _fstat(descriptor, phase, created)
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != trusted_owner_uid:
        _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
    _remove_inherited_acl(descriptor, _DEFAULT_ACL, phase, created)
    _remove_inherited_acl(descriptor, _ACCESS_ACL, phase, created)
    _fchmod(descriptor, 0o755, phase, created)
    observed = _fstat(descriptor, phase, created)
    if not _safe_directory_stat(observed, trusted_owner_uid) or stat.S_IMODE(observed.st_mode) != 0o755:
        _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
    _require_no_access_acl(descriptor, phase, created)


def _ensure_lock(
    parent_fd: int,
    trusted_owner_uid: int,
    created: list[FileLockSetupObject],
) -> None:
    phase = FileLockSetupPhase.LOCK
    parent = _fstat(parent_fd, phase, created)
    named = _stat_at(parent_fd, "files.lock", phase, created)
    created_now = False
    descriptor: int | None = None
    if named is None:
        descriptor = _create_lock(parent_fd, created)
        if descriptor is not None:
            created.append(FileLockSetupObject.LOCK)
            created_now = True
        else:
            named = _stat_at(parent_fd, "files.lock", phase, created)

    if descriptor is None:
        if named is None:
            _fail(FileLockSetupFailureKind.CONFLICT, phase, created)
        if not _safe_lock_stat(named, trusted_owner_uid, parent.st_dev):
            _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
        descriptor = _open_at(parent_fd, "files.lock", _LOCK_FLAGS, phase, created)

    try:
        if created_now:
            _finalize_new_lock(descriptor, trusted_owner_uid, parent.st_dev, created)
        opened = _fstat(descriptor, phase, created)
        named_after = _stat_at(parent_fd, "files.lock", phase, created)
        if not _safe_lock_stat(opened, trusted_owner_uid, parent.st_dev):
            _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
        if named_after is None or (named_after.st_dev, named_after.st_ino) != (opened.st_dev, opened.st_ino):
            _fail(FileLockSetupFailureKind.CONFLICT, phase, created)
        if not _safe_lock_stat(named_after, trusted_owner_uid, parent.st_dev):
            _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
        _require_no_access_acl(descriptor, phase, created)
    finally:
        _close(descriptor)


def _create_lock(parent_fd: int, created: list[FileLockSetupObject]) -> int | None:
    flags = _LOCK_FLAGS | os.O_CREAT | os.O_EXCL
    descriptor: int | None = None
    error_number: int | None = None
    try:
        descriptor = os.open("files.lock", flags, 0o444, dir_fd=parent_fd)
    except OSError as error:
        error_number = error.errno
    if descriptor is not None:
        return descriptor
    if error_number == errno.EEXIST:
        return None
    _fail(FileLockSetupFailureKind.IO, FileLockSetupPhase.LOCK, created)


def _finalize_new_lock(
    descriptor: int,
    trusted_owner_uid: int,
    parent_device: int,
    created: list[FileLockSetupObject],
) -> None:
    phase = FileLockSetupPhase.LOCK
    observed = _fstat(descriptor, phase, created)
    if not _safe_new_lock_shape(observed, trusted_owner_uid, parent_device):
        _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
    _remove_inherited_acl(descriptor, _ACCESS_ACL, phase, created)
    _fchmod(descriptor, 0o444, phase, created)
    observed = _fstat(descriptor, phase, created)
    if not _safe_lock_stat(observed, trusted_owner_uid, parent_device):
        _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
    _require_no_access_acl(descriptor, phase, created)


def _require_directory(
    descriptor: int,
    trusted_owner_uid: int,
    phase: FileLockSetupPhase,
    created: list[FileLockSetupObject],
) -> None:
    if not _safe_directory_stat(_fstat(descriptor, phase, created), trusted_owner_uid):
        _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
    _require_no_access_acl(descriptor, phase, created)


def _safe_directory_stat(observed: os.stat_result, trusted_owner_uid: int) -> bool:
    return (
        _owned_directory_stat(observed, trusted_owner_uid)
        and stat.S_IMODE(observed.st_mode) & 0o022 == 0
        and bool(observed.st_mode & stat.S_IXOTH)
    )


def _owned_directory_stat(observed: os.stat_result, trusted_owner_uid: int) -> bool:
    return stat.S_ISDIR(observed.st_mode) and observed.st_uid == trusted_owner_uid


def _safe_new_lock_shape(observed: os.stat_result, trusted_owner_uid: int, parent_device: int) -> bool:
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_nlink == 1
        and observed.st_uid == trusted_owner_uid
        and observed.st_size == 0
        and observed.st_dev == parent_device
    )


def _safe_lock_stat(observed: os.stat_result, trusted_owner_uid: int, parent_device: int) -> bool:
    return _safe_new_lock_shape(observed, trusted_owner_uid, parent_device) and stat.S_IMODE(observed.st_mode) == 0o444


def _require_no_access_acl(
    descriptor: int,
    phase: FileLockSetupPhase,
    created: list[FileLockSetupObject],
) -> None:
    present = False
    error_number: int | None = None
    try:
        os.getxattr(descriptor, _ACCESS_ACL)
    except OSError as error:
        error_number = error.errno
    else:
        present = True
    if present:
        _fail(FileLockSetupFailureKind.UNSAFE, phase, created)
    if error_number not in _absent_xattr_errors():
        _fail(FileLockSetupFailureKind.IO, phase, created)


def _remove_inherited_acl(
    descriptor: int,
    attribute: str,
    phase: FileLockSetupPhase,
    created: list[FileLockSetupObject],
) -> None:
    error_number: int | None = None
    try:
        os.removexattr(descriptor, attribute)
    except OSError as error:
        error_number = error.errno
    if error_number is not None and error_number not in _absent_xattr_errors():
        _fail(FileLockSetupFailureKind.IO, phase, created)


def _absent_xattr_errors() -> set[int]:
    return {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}


def _fchmod(
    descriptor: int,
    mode: int,
    phase: FileLockSetupPhase,
    created: list[FileLockSetupObject],
) -> None:
    failed = False
    try:
        os.fchmod(descriptor, mode)
    except OSError:
        failed = True
    if failed:
        _fail(FileLockSetupFailureKind.IO, phase, created)


def _stat_at(
    parent_fd: int,
    name: str,
    phase: FileLockSetupPhase,
    created: list[FileLockSetupObject],
) -> os.stat_result | None:
    observed: os.stat_result | None = None
    error_number: int | None = None
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        error_number = error.errno
    if observed is not None:
        return observed
    if error_number == errno.ENOENT:
        return None
    _fail(FileLockSetupFailureKind.IO, phase, created)


def _open_at(
    parent_fd: int,
    name: str,
    flags: int,
    phase: FileLockSetupPhase,
    created: list[FileLockSetupObject],
) -> int:
    descriptor: int | None = None
    error_number: int | None = None
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        error_number = error.errno
    if descriptor is not None:
        return descriptor
    if error_number in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
        _fail(FileLockSetupFailureKind.CONFLICT, phase, created)
    _fail(FileLockSetupFailureKind.IO, phase, created)


def _fstat(
    descriptor: int,
    phase: FileLockSetupPhase,
    created: list[FileLockSetupObject],
) -> os.stat_result:
    observed: os.stat_result | None = None
    with suppress(OSError):
        observed = os.fstat(descriptor)
    if observed is None:
        _fail(FileLockSetupFailureKind.IO, phase, created)
    return observed


def _fail(
    kind: FileLockSetupFailureKind,
    phase: FileLockSetupPhase,
    created: list[FileLockSetupObject],
) -> NoReturn:
    raise FileLockSetupError(kind, phase, created=tuple(created))


def _close(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)
