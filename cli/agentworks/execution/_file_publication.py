"""Atomic Linux publication beneath a caller-owned directory descriptor.
The caller owns confinement and locking; unsupported metadata and objects refuse.
"""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat
import sys
from contextlib import suppress
from dataclasses import astuple, dataclass
from enum import Enum
from operator import attrgetter
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from agentworks.execution._file_snapshot import FileSnapshot, SnapshotFailureKind, SnapshotReadError, read_snapshot

_ACCESS_ACL = "system.posix_acl_access"
_RENAME_NOREPLACE = 1
_STAGE_ATTEMPTS = 16
_STAGE_PREFIX = ".agentworks-stage-"
_T = TypeVar("_T")
_STAT_VALUES = attrgetter(
    "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns"
)


class PublicationFailureKind(Enum):
    """Closed publication outcomes that reveal no file content or names."""

    UNSUPPORTED = "unsupported"
    CONFLICT = "conflict"
    WRITE_AUTHORITY = "write_authority"
    METADATA = "metadata"
    IO = "io"
    UNCERTAIN = "uncertain"


class PublicationPhase(Enum):
    """The fixed phase in which publication stopped."""

    STAGING = "staging"
    CONTENT = "content"
    CONDITION = "condition"
    METADATA = "metadata"
    PUBLICATION = "publication"


class FilePublicationError(Exception):
    """A closed failure with optional bounded staging-cleanup debt."""

    def __init__(
        self,
        kind: PublicationFailureKind,
        phase: PublicationPhase,
        *,
        cleanup_failed: bool = False,
    ) -> None:
        self.kind = kind
        self.phase = phase
        self.cleanup_failed = cleanup_failed
        super().__init__(kind.value, phase.value, cleanup_failed)


@dataclass(frozen=True)
class CreateMetadata:
    """Numeric access metadata established before create-only publication."""

    uid: int
    gid: int
    mode: int

    def __post_init__(self) -> None:
        if type(self.uid) is not int or self.uid < 0:
            raise ValueError("Create UID must be a nonnegative integer")
        if type(self.gid) is not int or self.gid < 0:
            raise ValueError("Create GID must be a nonnegative integer")
        if type(self.mode) is not int or self.mode < 0 or self.mode & ~0o777:
            raise ValueError("Create mode must contain only regular permission bits")


@dataclass(frozen=True)
class _ObjectIdentity:
    device: int
    inode: int


def publish_file(
    parent_fd: int,
    leaf_name: str,
    content: bytes,
    *,
    expected: FileSnapshot | None,
    create_metadata: CreateMetadata,
) -> None:
    """Create an absent leaf or replace the exact supplied snapshot atomically.
    The parent is borrowed; replacement preserves access metadata, while creation
    establishes supplied metadata and normal default-ACL behavior.
    """
    _validate_inputs(parent_fd, leaf_name, content, expected, create_metadata)

    stage_name: str | None = None
    stage_identity: _ObjectIdentity | None = None
    failure: FilePublicationError | None = None
    try:
        stage_name, stage_fd, stage_identity = _create_stage(parent_fd)
        replacement_fd: int | None = None
        access_acl: bytes | None = None
        try:
            _write_all(stage_fd, content)
            if expected is None:
                _prepare_create(stage_fd, parent_fd, create_metadata)
            else:
                if expected.stat.mode & (stat.S_ISUID | stat.S_ISGID):
                    raise FilePublicationError(PublicationFailureKind.METADATA, PublicationPhase.METADATA)
                replacement_fd = _open_existing(parent_fd, leaf_name)
                _require_supported_existing(replacement_fd, expected.stat.device)
                _require_stat_match(replacement_fd, expected)
                access_acl = _read_access_acl(replacement_fd)
                _prepare_replacement(stage_fd, expected, access_acl)
            _os_call(os.fsync, stage_fd, kind=PublicationFailureKind.IO, phase=PublicationPhase.METADATA)
            _verify_stage(stage_fd, parent_fd, stage_name, stage_identity)
            if expected is None:
                _rename_noreplace(parent_fd, stage_name, leaf_name)
            else:
                assert replacement_fd is not None
                _verify_replacement_condition(parent_fd, leaf_name, expected, replacement_fd, access_acl)
                _rename_replace(parent_fd, stage_name, leaf_name)
        finally:
            if replacement_fd is not None:
                _close(replacement_fd)
            _close(stage_fd)
    except FilePublicationError as error:
        failure = error
    except BaseException:
        if stage_name is not None and stage_identity is not None:
            _cleanup_stage(parent_fd, stage_name, stage_identity)
        raise
    else:
        return

    cleanup_failed = False
    if stage_name is not None and stage_identity is not None:
        cleanup_failed = not _cleanup_stage(parent_fd, stage_name, stage_identity)
    assert failure is not None
    raise FilePublicationError(
        failure.kind,
        failure.phase,
        cleanup_failed=failure.cleanup_failed or cleanup_failed,
    ) from None


def _validate_inputs(
    parent_fd: int,
    leaf_name: str,
    content: bytes,
    expected: FileSnapshot | None,
    create_metadata: CreateMetadata,
) -> None:
    if type(parent_fd) is not int or parent_fd < 0:
        raise ValueError("Parent descriptor must be a nonnegative integer")
    if type(leaf_name) is not str or not leaf_name or leaf_name in {".", ".."}:
        raise ValueError("Publication leaf must be a simple nonempty name")
    if "/" in leaf_name or "\x00" in leaf_name:
        raise ValueError("Publication leaf must be a simple nonempty name")
    encoding_failed = False
    try:
        os.fsencode(leaf_name)
    except UnicodeEncodeError:
        encoding_failed = True
    if encoding_failed:
        raise ValueError("Publication leaf is not encodable")
    if type(content) is not bytes:
        raise ValueError("Publication content must be bytes")
    if expected is not None and not isinstance(expected, FileSnapshot):
        raise ValueError("Expected state must be a file snapshot or None")
    if not isinstance(create_metadata, CreateMetadata):
        raise ValueError("Create metadata has an invalid type")
    if sys.platform != "linux":
        raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.STAGING)


def _create_stage(parent_fd: int) -> tuple[str, int, _ObjectIdentity]:
    parent = _fstat(parent_fd, PublicationPhase.STAGING)
    if not stat.S_ISDIR(parent.st_mode):
        raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.STAGING)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(_STAGE_ATTEMPTS):
        name = _STAGE_PREFIX + secrets.token_hex(16)
        error_number: int | None = None
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except OSError as error:
            error_number = error.errno
        else:
            observed: os.stat_result | None = None
            with suppress(OSError):
                observed = os.fstat(descriptor)
            if observed is None:
                _close(descriptor)
                raise FilePublicationError(
                    PublicationFailureKind.IO,
                    PublicationPhase.STAGING,
                    cleanup_failed=True,
                )
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or observed.st_dev != parent.st_dev:
                _close(descriptor)
                identity = _ObjectIdentity(observed.st_dev, observed.st_ino)
                cleanup_failed = not _cleanup_stage(parent_fd, name, identity)
                raise FilePublicationError(
                    PublicationFailureKind.UNSUPPORTED,
                    PublicationPhase.STAGING,
                    cleanup_failed=cleanup_failed,
                )
            return name, descriptor, _ObjectIdentity(observed.st_dev, observed.st_ino)
        if error_number != errno.EEXIST:
            raise FilePublicationError(PublicationFailureKind.IO, PublicationPhase.STAGING)
    raise FilePublicationError(PublicationFailureKind.IO, PublicationPhase.STAGING)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    offset = 0
    while offset < len(view):
        written = _write(descriptor, view[offset:])
        if written <= 0 or written > len(view) - offset:
            raise FilePublicationError(PublicationFailureKind.IO, PublicationPhase.CONTENT)
        offset += written


def _write(descriptor: int, content: memoryview) -> int:
    return _os_call(os.write, descriptor, content, kind=PublicationFailureKind.IO, phase=PublicationPhase.CONTENT)


def _prepare_create(
    stage_fd: int,
    parent_fd: int,
    metadata: CreateMetadata,
) -> None:
    _require_known_metadata(stage_fd)
    _set_ownership(stage_fd, metadata.uid, metadata.gid)
    _metadata_call(os.fchmod, stage_fd, metadata.mode)
    observed = _fstat(stage_fd, PublicationPhase.METADATA)
    parent = _fstat(parent_fd, PublicationPhase.METADATA)
    if not _matches_access_metadata(observed, metadata.uid, metadata.gid, metadata.mode, parent.st_dev):
        raise FilePublicationError(PublicationFailureKind.METADATA, PublicationPhase.METADATA)
    _require_known_metadata(stage_fd)


def _prepare_replacement(stage_fd: int, expected: FileSnapshot, access_acl: bytes | None) -> None:
    _require_known_metadata(stage_fd)
    mode = stat.S_IMODE(expected.stat.mode)
    _set_ownership(stage_fd, expected.stat.uid, expected.stat.gid)
    _set_access_acl(stage_fd, access_acl)
    _metadata_call(os.fchmod, stage_fd, mode)
    observed = _fstat(stage_fd, PublicationPhase.METADATA)
    if not _matches_access_metadata(observed, expected.stat.uid, expected.stat.gid, mode, expected.stat.device):
        raise FilePublicationError(PublicationFailureKind.METADATA, PublicationPhase.METADATA)
    if _read_access_acl(stage_fd) != access_acl:
        raise FilePublicationError(PublicationFailureKind.METADATA, PublicationPhase.METADATA)


def _read_matching_snapshot(parent_fd: int, leaf_name: str, expected: FileSnapshot) -> FileSnapshot | None:
    result: FileSnapshot | None = None
    failure_kind: SnapshotFailureKind | None = None
    try:
        result = read_snapshot(parent_fd, leaf_name, max(expected.stat.size, 1))
    except SnapshotReadError as error:
        failure_kind = error.kind
    if failure_kind is None:
        return result
    if failure_kind is SnapshotFailureKind.UNSUPPORTED_OBJECT:
        raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.CONDITION)
    if failure_kind in {SnapshotFailureKind.CONFLICT, SnapshotFailureKind.LIMIT}:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)
    raise FilePublicationError(PublicationFailureKind.IO, PublicationPhase.CONDITION)


def _open_existing(parent_fd: int, leaf_name: str) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    error_number: int | None = None
    try:
        descriptor = os.open(leaf_name, flags, dir_fd=parent_fd)
    except OSError as error:
        error_number = error.errno
    else:
        return descriptor
    if error_number in {errno.EACCES, errno.EPERM, errno.EROFS}:
        raise FilePublicationError(PublicationFailureKind.WRITE_AUTHORITY, PublicationPhase.CONDITION)
    if error_number in {errno.EISDIR, errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.ENODEV}:
        raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.CONDITION)
    if error_number == errno.ENOENT:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)
    raise FilePublicationError(PublicationFailureKind.IO, PublicationPhase.CONDITION)


def _require_stat_match(descriptor: int, expected: FileSnapshot) -> None:
    observed = _fstat(descriptor, PublicationPhase.CONDITION)
    if _STAT_VALUES(observed) != astuple(expected.stat):
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)


def _require_supported_existing(descriptor: int, device: int) -> None:
    observed = _fstat(descriptor, PublicationPhase.CONDITION)
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or observed.st_dev != device:
        raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.CONDITION)


def _verify_replacement_condition(
    parent_fd: int,
    leaf_name: str,
    expected: FileSnapshot,
    replacement_fd: int,
    access_acl: bytes | None,
) -> None:
    current = _read_matching_snapshot(parent_fd, leaf_name, expected)
    if current is None or current != expected:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)
    _require_stat_match(replacement_fd, expected)
    if _read_access_acl(replacement_fd) != access_acl:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)


def _read_access_acl(descriptor: int) -> bytes | None:
    names = _require_known_metadata(descriptor)
    if _ACCESS_ACL not in names:
        return None
    return _metadata_call(os.getxattr, descriptor, _ACCESS_ACL)


def _require_known_metadata(descriptor: int) -> set[str]:
    names = set(_metadata_call(os.listxattr, descriptor))
    if names - {_ACCESS_ACL}:
        raise FilePublicationError(PublicationFailureKind.METADATA, PublicationPhase.METADATA)
    return names


def _set_access_acl(descriptor: int, access_acl: bytes | None) -> None:
    existing = _require_known_metadata(descriptor)
    if access_acl is None:
        if _ACCESS_ACL in existing:
            _metadata_call(os.removexattr, descriptor, _ACCESS_ACL)
    else:
        _metadata_call(os.setxattr, descriptor, _ACCESS_ACL, access_acl)


def _set_ownership(descriptor: int, uid: int, gid: int) -> None:
    observed = _fstat(descriptor, PublicationPhase.METADATA)
    if (observed.st_uid, observed.st_gid) != (uid, gid):
        _metadata_call(os.fchown, descriptor, uid, gid)


def _matches_access_metadata(observed: os.stat_result, uid: int, gid: int, mode: int, device: int) -> bool:
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_nlink == 1
        and observed.st_dev == device
        and observed.st_uid == uid
        and observed.st_gid == gid
        and stat.S_IMODE(observed.st_mode) == mode
    )


def _verify_stage(
    stage_fd: int,
    parent_fd: int,
    stage_name: str,
    identity: _ObjectIdentity,
) -> None:
    descriptor_stat = _fstat(stage_fd, PublicationPhase.METADATA)
    named_stat = _stat_at(parent_fd, stage_name, PublicationPhase.METADATA)
    if (
        named_stat is None
        or not stat.S_ISREG(descriptor_stat.st_mode)
        or descriptor_stat.st_nlink != 1
        or _ObjectIdentity(descriptor_stat.st_dev, descriptor_stat.st_ino) != identity
        or _ObjectIdentity(named_stat.st_dev, named_stat.st_ino) != identity
        or _STAT_VALUES(named_stat) != _STAT_VALUES(descriptor_stat)
    ):
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.METADATA)


def _rename_noreplace(parent_fd: int, stage_name: str, leaf_name: str) -> None:
    renameat2 = None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
    except OSError:
        renameat2 = None
    if renameat2 is None:
        raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.PUBLICATION)
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    interrupted = False
    try:
        result = renameat2(
            parent_fd,
            os.fsencode(stage_name),
            parent_fd,
            os.fsencode(leaf_name),
            _RENAME_NOREPLACE,
        )
    except BaseException:
        interrupted = True
        result = -1
    if interrupted:
        raise FilePublicationError(PublicationFailureKind.UNCERTAIN, PublicationPhase.PUBLICATION)
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.PUBLICATION)
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
        raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.PUBLICATION)
    raise FilePublicationError(PublicationFailureKind.UNCERTAIN, PublicationPhase.PUBLICATION)


def _rename_replace(parent_fd: int, stage_name: str, leaf_name: str) -> None:
    failed = False
    try:
        os.rename(stage_name, leaf_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException:
        failed = True
    if failed:
        raise FilePublicationError(PublicationFailureKind.UNCERTAIN, PublicationPhase.PUBLICATION)


def _cleanup_stage(parent_fd: int, stage_name: str, identity: _ObjectIdentity) -> bool:
    try:
        observed = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if _ObjectIdentity(observed.st_dev, observed.st_ino) != identity:
        return False
    failed = False
    try:
        os.unlink(stage_name, dir_fd=parent_fd)
    except OSError:
        failed = True
    return not failed


def _fstat(descriptor: int, phase: PublicationPhase) -> os.stat_result:
    return _os_call(os.fstat, descriptor, kind=PublicationFailureKind.IO, phase=phase)


def _stat_at(parent_fd: int, name: str, phase: PublicationPhase) -> os.stat_result | None:
    error_number: int | None = None
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        error_number = error.errno
        observed = None
    if error_number == errno.ENOENT:
        return None
    if error_number is not None or observed is None:
        raise FilePublicationError(PublicationFailureKind.IO, phase)
    return observed


def _metadata_call(function: Callable[..., _T], *args: object) -> _T:  # noqa: UP047
    return _os_call(
        function,
        *args,
        kind=PublicationFailureKind.METADATA,
        phase=PublicationPhase.METADATA,
    )


def _os_call(  # noqa: UP047
    function: Callable[..., _T],
    *args: object,
    kind: PublicationFailureKind,
    phase: PublicationPhase,
    **kwargs: object,
) -> _T:
    failed = False
    result: object = None
    try:
        result = function(*args, **kwargs)
    except OSError:
        failed = True
    if failed:
        raise FilePublicationError(kind, phase)
    return cast("_T", result)


def _close(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)
