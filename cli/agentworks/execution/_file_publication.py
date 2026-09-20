"""Atomic Linux publication beneath a caller-owned directory descriptor.
The caller owns confinement and locking; unsupported metadata and objects refuse.
Operational facts are carried by ``FilePublicationError`` directly or as the
fixed cause of an escaping control-flow exception.
Known descriptors are protected by an encompassing ``finally``; Python-level
ownership assignment and failure bookkeeping are not signal-atomic.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import os
import secrets
import stat
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, NoReturn, Protocol, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from ._file_snapshot import _snapshot_stat
from ._file_stat import FileRevision, FileStat
from ._scratch import (
    ReadyScratchReference,
    ScratchFailureKind,
    ScratchTransferError,
    iter_ready_scratch,
    ready_scratch_contract,
)

_ACCESS_ACL = "system.posix_acl_access"
_RENAME_NOREPLACE = 1
_STAGE_ATTEMPTS = 16
_STAGE_PREFIX = ".agentworks-stage-"
_T = TypeVar("_T")


class _Digest(Protocol):
    def update(self, data: bytes | bytearray | memoryview, /) -> None: ...

    def digest(self) -> bytes: ...


class PublicationFailureKind(Enum):
    """Closed publication outcomes that reveal no file content or names."""

    UNSUPPORTED = "unsupported"
    CONFLICT = "conflict"
    WRITE_AUTHORITY = "write_authority"
    METADATA = "metadata"
    DEADLINE = "deadline"
    IO = "io"
    UNCERTAIN = "uncertain"


class PublicationPhase(Enum):
    """The fixed phase in which publication stopped."""

    STAGING = "staging"
    CONTENT = "content"
    CONDITION = "condition"
    METADATA = "metadata"
    PUBLICATION = "publication"
    CLEANUP = "cleanup"


@dataclass(frozen=True, repr=False)
class PublicationCleanupDebt:
    """Exact private staging identity retained after cleanup refusal."""

    name: str = field(repr=False)
    device: int | None = field(repr=False)
    inode: int | None = field(repr=False)


class FilePublicationError(Exception):
    """A closed failure with optional bounded staging-cleanup debt."""

    def __init__(
        self,
        kind: PublicationFailureKind,
        phase: PublicationPhase,
        *,
        cleanup_debt: PublicationCleanupDebt | None = None,
    ) -> None:
        self.kind = kind
        self.phase = phase
        self.cleanup_debt = cleanup_debt
        super().__init__(kind.value, phase.value, cleanup_debt is not None)


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


@dataclass(frozen=True, slots=True)
class Create:
    """Require an absent destination."""


@dataclass(frozen=True, slots=True)
class Replace:
    """Require an existing regular file without matching a prior revision."""


@dataclass(frozen=True, slots=True)
class Match:
    """Require an existing regular file matching one prior revision."""

    revision: FileRevision


@dataclass(frozen=True, slots=True, repr=False)
class ScratchFileSource:
    """Borrowed parent descriptor and verified private scratch object."""

    parent_fd: int
    ready: ReadyScratchReference


@dataclass(frozen=True)
class _ObjectIdentity:
    device: int
    inode: int


@dataclass(repr=False)
class _StageOwnership:
    """Staging resources acquired by one publication attempt."""

    name: str | None = None
    descriptor: int | None = None
    identity: _ObjectIdentity | None = None


@dataclass(frozen=True, repr=False)
class _ReplacementObservation:
    descriptor: int
    revision: FileRevision
    access_acl: bytes | None = field(repr=False)


def publish_file(
    parent_fd: int,
    leaf_name: str,
    content: bytes | ScratchFileSource,
    *,
    condition: Create | Replace | Match,
    create_metadata: CreateMetadata,
    expires_at: float | None = None,
) -> FileRevision:
    """Publish bytes or verified scratch under one explicit write condition.

    The parent and cooperating-writer lock are borrowed. Replacement preserves
    the observed access profile and refuses a change seen before publication.
    """
    _validate_inputs(parent_fd, leaf_name, content, condition, create_metadata, expires_at)
    _check_deadline(expires_at, PublicationPhase.STAGING)

    stage = _StageOwnership()
    replacement: _ReplacementObservation | None = None
    publication_attempted = False
    rename_succeeded = False
    published_revision: FileRevision | None = None
    failure: FilePublicationError | None = None
    control: BaseException | None = None
    close_control: BaseException | None = None
    try:
        try:
            _create_stage(parent_fd, stage)
            stage_name = stage.name
            stage_fd = stage.descriptor
            stage_identity = stage.identity
            assert stage_name is not None and stage_fd is not None and stage_identity is not None
            content_size, content_digest = _write_content(stage_fd, content, expires_at)
            if isinstance(condition, Create):
                _prepare_create(stage_fd, parent_fd, create_metadata)
            else:
                replacement = _observe_replacement(parent_fd, leaf_name, condition, expires_at)
                if replacement.revision.stat.mode & (stat.S_ISUID | stat.S_ISGID):
                    raise FilePublicationError(PublicationFailureKind.METADATA, PublicationPhase.METADATA)
                _prepare_replacement(stage_fd, replacement.revision.stat, replacement.access_acl)
            _os_call(os.fsync, stage_fd, kind=PublicationFailureKind.IO, phase=PublicationPhase.METADATA)
            _verify_stage(stage_fd, parent_fd, stage_name, stage_identity, content_size)
            _check_deadline(expires_at, PublicationPhase.PUBLICATION)
            if isinstance(condition, Create):
                publication_attempted = True
                _rename_noreplace(parent_fd, stage_name, leaf_name)
                rename_succeeded = True
            else:
                assert replacement is not None
                _verify_replacement_condition(parent_fd, leaf_name, replacement, expires_at)
                _check_deadline(expires_at, PublicationPhase.PUBLICATION)
                publication_attempted = True
                _rename_replace(parent_fd, stage_name, leaf_name)
                rename_succeeded = True
            published_revision = _observe_published(
                stage_fd,
                parent_fd,
                leaf_name,
                stage_identity,
                content_digest,
                expires_at,
            )
        except FilePublicationError as error:
            if rename_succeeded or (
                publication_attempted
                and error.kind
                not in {
                    PublicationFailureKind.CONFLICT,
                    PublicationFailureKind.UNSUPPORTED,
                }
            ):
                failure = FilePublicationError(PublicationFailureKind.UNCERTAIN, PublicationPhase.PUBLICATION)
            else:
                failure = error
        except BaseException as error:
            control = error
    finally:
        close_control = _close_publication_descriptors(
            None if replacement is None else replacement.descriptor,
            stage,
        )

    prior = failure or (_control_fact(control) if control is not None else None)
    if prior is None and publication_attempted and (published_revision is None or close_control is not None):
        prior = FilePublicationError(PublicationFailureKind.UNCERTAIN, PublicationPhase.PUBLICATION)
    if close_control is not None:
        control = close_control

    if failure is None and control is None:
        assert published_revision is not None
        return published_revision

    cleanup_debt = _cleanup_owned_stage(parent_fd, stage, prior=prior)
    if control is not None:
        _raise_control(control, prior=prior, cleanup_debt=cleanup_debt)

    assert failure is not None
    raise FilePublicationError(
        failure.kind,
        failure.phase,
        cleanup_debt=failure.cleanup_debt or cleanup_debt,
    ) from None


def retry_publication_cleanup(parent_fd: int, debt: PublicationCleanupDebt) -> bool:
    """Retry exact-name cleanup under the same borrowed parent descriptor."""
    if not isinstance(debt, PublicationCleanupDebt):
        raise ValueError("Publication cleanup debt has an invalid type")
    if debt.device is None or debt.inode is None:
        return False
    return _cleanup_stage(parent_fd, debt.name, _ObjectIdentity(debt.device, debt.inode))


def _validate_inputs(
    parent_fd: int,
    leaf_name: str,
    content: bytes | ScratchFileSource,
    condition: Create | Replace | Match,
    create_metadata: CreateMetadata,
    expires_at: float | None,
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
    if type(content) is not bytes and not isinstance(content, ScratchFileSource):
        raise ValueError("Publication content must be bytes or verified scratch")
    if not isinstance(condition, (Create, Replace, Match)):
        raise ValueError("Publication condition has an invalid type")
    if not isinstance(create_metadata, CreateMetadata):
        raise ValueError("Create metadata has an invalid type")
    if expires_at is not None and (type(expires_at) not in {int, float} or expires_at != expires_at):
        raise ValueError("Publication deadline must be a monotonic timestamp or None")
    if sys.platform != "linux":
        raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.STAGING)


def _create_stage(parent_fd: int, stage: _StageOwnership) -> None:
    """Acquire a stage and populate caller-owned state immediately.

    Pure Python cannot cover an asynchronous exception between ``os.open``
    returning from the kernel and assignment of its result to ``stage``. Such
    an exception can leave an untracked staging object, so this function does
    not claim complete single-interrupt atomicity across that bytecode window.
    """
    parent = _fstat(parent_fd, PublicationPhase.STAGING)
    if not stat.S_ISDIR(parent.st_mode):
        raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.STAGING)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(_STAGE_ATTEMPTS):
        candidate = _STAGE_PREFIX + secrets.token_hex(16)
        stage.name = candidate
        error_number: int | None = None
        try:
            stage.descriptor = os.open(candidate, flags, 0o600, dir_fd=parent_fd)
        except OSError as error:
            error_number = error.errno
        else:
            assert stage.descriptor is not None
            observed = _fstat(stage.descriptor, PublicationPhase.STAGING)
            stage.identity = _ObjectIdentity(observed.st_dev, observed.st_ino)
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or observed.st_dev != parent.st_dev:
                raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.STAGING)
            return
        stage.name = None
        if error_number != errno.EEXIST:
            raise FilePublicationError(PublicationFailureKind.IO, PublicationPhase.STAGING)
    raise FilePublicationError(PublicationFailureKind.IO, PublicationPhase.STAGING)


def _write_content(
    descriptor: int,
    content: bytes | ScratchFileSource,
    expires_at: float | None,
) -> tuple[int, bytes]:
    digest = hashlib.sha256()
    copied = 0
    if isinstance(content, ScratchFileSource):
        expected_size, expected_digest = ready_scratch_contract(content.ready)
        try:
            chunks = iter(iter_ready_scratch(content.parent_fd, content.ready, expires_at=expires_at))
            while True:
                _check_deadline(expires_at, PublicationPhase.CONTENT)
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                _check_deadline(expires_at, PublicationPhase.CONTENT)
                _write_block(descriptor, chunk, digest, expires_at=expires_at)
                copied += len(chunk)
        except ScratchTransferError as error:
            if error.kind is ScratchFailureKind.UNSUPPORTED:
                kind = PublicationFailureKind.UNSUPPORTED
            elif error.kind is ScratchFailureKind.DEADLINE:
                kind = PublicationFailureKind.DEADLINE
            elif error.kind is ScratchFailureKind.IO:
                kind = PublicationFailureKind.IO
            else:
                kind = PublicationFailureKind.CONFLICT
            raise FilePublicationError(kind, PublicationPhase.CONTENT) from None
        if copied != expected_size or not hmac.compare_digest(digest.digest(), expected_digest):
            raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONTENT)
        return copied, digest.digest()

    _write_block(descriptor, content, digest, expires_at=expires_at)
    return len(content), digest.digest()


def _write_block(
    descriptor: int,
    content: bytes,
    digest: _Digest,
    *,
    expires_at: float | None = None,
) -> None:
    view = memoryview(content)
    offset = 0
    while offset < len(view):
        _check_deadline(expires_at, PublicationPhase.CONTENT)
        request = view[offset : offset + 64 * 1024]
        written = _write(descriptor, request)
        if written <= 0 or written > len(request):
            raise FilePublicationError(PublicationFailureKind.IO, PublicationPhase.CONTENT)
        digest.update(request[:written])
        offset += written


def _write(descriptor: int, content: memoryview) -> int:
    return _os_call(os.write, descriptor, content, kind=PublicationFailureKind.IO, phase=PublicationPhase.CONTENT)


def _pread(descriptor: int, size: int, offset: int) -> bytes:
    return _os_call(
        os.pread,
        descriptor,
        size,
        offset,
        kind=PublicationFailureKind.IO,
        phase=PublicationPhase.CONDITION,
    )


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


def _prepare_replacement(stage_fd: int, expected: FileStat, access_acl: bytes | None) -> None:
    _require_known_metadata(stage_fd)
    mode = stat.S_IMODE(expected.mode)
    _set_ownership(stage_fd, expected.uid, expected.gid)
    _set_access_acl(stage_fd, access_acl)
    _metadata_call(os.fchmod, stage_fd, mode)
    observed = _fstat(stage_fd, PublicationPhase.METADATA)
    if not _matches_access_metadata(observed, expected.uid, expected.gid, mode, expected.device):
        raise FilePublicationError(PublicationFailureKind.METADATA, PublicationPhase.METADATA)
    if _read_access_acl(stage_fd) != access_acl:
        raise FilePublicationError(PublicationFailureKind.METADATA, PublicationPhase.METADATA)


def _open_existing(parent_fd: int, leaf_name: str, device: int, *, readable: bool) -> int:
    observed = _stat_at(parent_fd, leaf_name, PublicationPhase.CONDITION)
    if observed is None:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or observed.st_dev != device:
        raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.CONDITION)
    flags = (os.O_RDWR if readable else os.O_WRONLY) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
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


def _observe_replacement(
    parent_fd: int,
    leaf_name: str,
    condition: Replace | Match,
    expires_at: float | None,
) -> _ReplacementObservation:
    parent = _fstat(parent_fd, PublicationPhase.CONDITION)
    named = _stat_at(parent_fd, leaf_name, PublicationPhase.CONDITION)
    if named is None:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)
    _require_supported_regular(named, parent.st_dev)
    include_digest = isinstance(condition, Match) and condition.revision.digest is not None
    descriptor = _open_existing(parent_fd, leaf_name, parent.st_dev, readable=include_digest)
    try:
        revision = _observe_descriptor_revision(
            descriptor,
            parent_fd,
            leaf_name,
            parent.st_dev,
            include_digest=include_digest,
            expires_at=expires_at,
        )
        if isinstance(condition, Match) and revision != condition.revision:
            raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)
        access_acl = _read_access_acl(descriptor)
        _require_revision_match(descriptor, parent_fd, leaf_name, revision)
    except BaseException:
        _close(descriptor)
        raise
    return _ReplacementObservation(descriptor, revision, access_acl)


def _observe_descriptor_revision(
    descriptor: int,
    parent_fd: int,
    leaf_name: str,
    device: int,
    *,
    include_digest: bool,
    expires_at: float | None,
) -> FileRevision:
    before_result = _fstat(descriptor, PublicationPhase.CONDITION)
    _require_supported_regular(before_result, device)
    before = _snapshot_stat(before_result)
    named = _stat_at(parent_fd, leaf_name, PublicationPhase.CONDITION)
    if named is None or _snapshot_stat(named) != before:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)
    digest = hashlib.sha256() if include_digest else None
    observed_size = 0
    while digest is not None and observed_size < before.size:
        _check_deadline(expires_at, PublicationPhase.CONDITION)
        block = _pread(descriptor, min(64 * 1024, before.size - observed_size), observed_size)
        if not block:
            raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)
        digest.update(block)
        observed_size += len(block)
    _check_deadline(expires_at, PublicationPhase.CONDITION)
    after = _snapshot_stat(_fstat(descriptor, PublicationPhase.CONDITION))
    named_after = _stat_at(parent_fd, leaf_name, PublicationPhase.CONDITION)
    if after != before or named_after is None or _snapshot_stat(named_after) != before:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)
    return FileRevision(before, None if digest is None else digest.digest())


def _require_supported_regular(observed: os.stat_result, device: int) -> None:
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or observed.st_dev != device:
        raise FilePublicationError(PublicationFailureKind.UNSUPPORTED, PublicationPhase.CONDITION)


def _require_revision_match(
    descriptor: int,
    parent_fd: int,
    leaf_name: str,
    revision: FileRevision,
) -> None:
    observed = _fstat(descriptor, PublicationPhase.CONDITION)
    _require_supported_regular(observed, revision.stat.device)
    named = _stat_at(parent_fd, leaf_name, PublicationPhase.CONDITION)
    if _snapshot_stat(observed) != revision.stat or named is None or _snapshot_stat(named) != revision.stat:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)


def _verify_replacement_condition(
    parent_fd: int,
    leaf_name: str,
    replacement: _ReplacementObservation,
    expires_at: float | None,
) -> None:
    current = _observe_descriptor_revision(
        replacement.descriptor,
        parent_fd,
        leaf_name,
        replacement.revision.stat.device,
        include_digest=replacement.revision.digest is not None,
        expires_at=expires_at,
    )
    if current != replacement.revision:
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.CONDITION)
    if _read_access_acl(replacement.descriptor) != replacement.access_acl:
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
    content_size: int,
) -> None:
    descriptor_stat = _fstat(stage_fd, PublicationPhase.METADATA)
    named_stat = _stat_at(parent_fd, stage_name, PublicationPhase.METADATA)
    if (
        named_stat is None
        or not stat.S_ISREG(descriptor_stat.st_mode)
        or descriptor_stat.st_nlink != 1
        or descriptor_stat.st_size != content_size
        or _ObjectIdentity(descriptor_stat.st_dev, descriptor_stat.st_ino) != identity
        or _ObjectIdentity(named_stat.st_dev, named_stat.st_ino) != identity
        or _snapshot_stat(named_stat) != _snapshot_stat(descriptor_stat)
    ):
        raise FilePublicationError(PublicationFailureKind.CONFLICT, PublicationPhase.METADATA)


def _observe_published(
    stage_fd: int,
    parent_fd: int,
    leaf_name: str,
    identity: _ObjectIdentity,
    digest: bytes,
    expires_at: float | None,
) -> FileRevision:
    _check_deadline(expires_at, PublicationPhase.PUBLICATION)
    revision = _observe_descriptor_revision(
        stage_fd,
        parent_fd,
        leaf_name,
        identity.device,
        include_digest=True,
        expires_at=expires_at,
    )
    assert revision.digest is not None
    if revision.stat.inode != identity.inode or not hmac.compare_digest(revision.digest, digest):
        raise FilePublicationError(PublicationFailureKind.UNCERTAIN, PublicationPhase.PUBLICATION)
    return revision


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
    failed = False
    try:
        result = renameat2(
            parent_fd,
            os.fsencode(stage_name),
            parent_fd,
            os.fsencode(leaf_name),
            _RENAME_NOREPLACE,
        )
    except OSError:
        failed = True
        result = -1
    if failed:
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
    except OSError:
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


def _cleanup_debt(stage_name: str, identity: _ObjectIdentity) -> PublicationCleanupDebt:
    return PublicationCleanupDebt(stage_name, identity.device, identity.inode)


def _cleanup_owned_stage(
    parent_fd: int,
    stage: _StageOwnership,
    *,
    prior: FilePublicationError | None,
) -> PublicationCleanupDebt | None:
    if stage.name is None:
        return None
    if stage.identity is None:
        return PublicationCleanupDebt(stage.name, None, None)
    return _cleanup_or_raise_control(parent_fd, stage.name, stage.identity, prior=prior)


def _close_publication_descriptors(
    replacement_fd: int | None,
    stage: _StageOwnership,
) -> BaseException | None:
    interrupted: BaseException | None = None
    if replacement_fd is not None:
        try:
            _close(replacement_fd)
        except BaseException as control:
            interrupted = control
    if stage.descriptor is not None:
        try:
            _close(stage.descriptor)
        except BaseException as control:
            interrupted = control
    return interrupted


def _cleanup_or_raise_control(
    parent_fd: int,
    stage_name: str,
    identity: _ObjectIdentity,
    *,
    prior: FilePublicationError | None,
) -> PublicationCleanupDebt | None:
    """Try cleanup once; a cleanup interruption escapes with the known debt.

    Repeated asynchronous interruption is not retried or bounded here.
    """
    debt = _cleanup_debt(stage_name, identity)
    interrupted: BaseException | None = None
    try:
        cleaned = _cleanup_stage(parent_fd, stage_name, identity)
    except BaseException as control:
        interrupted = control
        cleaned = False
    if interrupted is not None:
        _raise_control(interrupted, prior=prior, cleanup_debt=debt)
    return None if cleaned else debt


def _control_fact(control: BaseException) -> FilePublicationError | None:
    cause = control.__cause__
    return cause if isinstance(cause, FilePublicationError) else None


def _raise_control(
    control: BaseException,
    *,
    prior: FilePublicationError | None = None,
    cleanup_debt: PublicationCleanupDebt | None = None,
) -> NoReturn:
    fact = prior or _control_fact(control)
    if cleanup_debt is not None:
        if fact is None:
            fact = FilePublicationError(
                PublicationFailureKind.IO,
                PublicationPhase.CLEANUP,
                cleanup_debt=cleanup_debt,
            )
        else:
            fact = FilePublicationError(fact.kind, fact.phase, cleanup_debt=cleanup_debt)
    if fact is None:
        raise control
    raise control from fact


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
) -> _T:
    failed = False
    result: object = None
    try:
        result = function(*args)
    except OSError:
        failed = True
    if failed:
        raise FilePublicationError(kind, phase)
    return cast("_T", result)


def _check_deadline(expires_at: float | None, phase: PublicationPhase) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise FilePublicationError(PublicationFailureKind.DEADLINE, phase)


def _close(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)
