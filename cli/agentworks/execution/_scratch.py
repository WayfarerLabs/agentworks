"""Private POSIX scratch transfer beneath a caller-owned directory descriptor.

The caller owns confinement and operation deadlines. Every operation reopens
the recorded objects and refuses changed identity, type, links, owner, or mode.
Known descriptors receive bounded close handling, but Python ownership
assignment and cleanup bookkeeping are not signal-atomic. Filesystem calls do
not provide a hard interruption bound.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import NoReturn

_DATA_NAME = "data"
_DIRECTORY_MODE = 0o700
_OBJECT_MODE = 0o600
# Internal candidate pending whole-request carrier proof.
_MAX_CHUNK_BYTES = 24 * 1024
_MAX_OFFSET = (1 << 63) - 1
_NAME_ATTEMPTS = 16
_NAME_PREFIX = ".agentworks-scratch-"
_HASH_READ_BYTES = 64 * 1024


class ScratchFailureKind(Enum):
    """Closed failures that reveal no transferred bytes or scratch names."""

    UNSUPPORTED = "unsupported"
    CONFLICT = "conflict"
    LIMIT = "limit"
    INTEGRITY = "integrity"
    IO = "io"


class ScratchPhase(Enum):
    """The fixed scratch operation in which work stopped."""

    BEGIN = "begin"
    WRITE = "write"
    VERIFY = "verify"
    READ = "read"
    CLEANUP = "cleanup"


@dataclass(frozen=True, repr=False)
class _Identity:
    device: int
    inode: int


@dataclass(frozen=True, repr=False)
class ScratchCleanupDebt:
    """Exact private identity retained for bounded cleanup retry."""

    _name: str
    _directory: _Identity | None
    _object: _Identity | None
    _uid: int
    _gid: int


@dataclass(frozen=True, repr=False)
class ScratchReference:
    """Identity and integrity contract for one private scratch object."""

    _name: str
    _directory: _Identity
    _object: _Identity
    _uid: int
    _gid: int
    _length: int
    _digest: bytes


@dataclass(frozen=True, repr=False)
class ReadyScratchReference:
    """A scratch reference whose whole object was verified successfully."""

    _reference: ScratchReference
    _modified_ns: int
    _changed_ns: int


class ScratchTransferError(Exception):
    """A closed failure with optional exact cleanup debt."""

    def __init__(
        self,
        kind: ScratchFailureKind,
        phase: ScratchPhase,
        *,
        cleanup_debt: ScratchCleanupDebt | None = None,
    ) -> None:
        self.kind = kind
        self.phase = phase
        self.cleanup_debt = cleanup_debt
        super().__init__(kind.value, phase.value, cleanup_debt is not None)


@dataclass(repr=False)
class _ScratchAcquisition:
    uid: int
    gid: int
    name: str | None = None
    directory_fd: int | None = None
    object_fd: int | None = None
    directory: _Identity | None = None
    object: _Identity | None = None


@dataclass(repr=False)
class _OpenedScratch:
    directory_fd: int
    object_fd: int
    object_stat: os.stat_result

    def close(self) -> BaseException | None:
        return _close_descriptors(self.object_fd, self.directory_fd)


def begin_scratch(parent_fd: int, expected_length: int, expected_digest: bytes) -> ScratchReference:
    """Create one private directory and fixed data object beneath ``parent_fd``."""
    _validate_contract(expected_length, expected_digest)
    uid = os.geteuid()
    gid = os.getegid()
    acquisition = _ScratchAcquisition(uid, gid)
    failure: ScratchTransferError | None = None
    control: BaseException | None = None
    close_control: BaseException | None = None
    result: ScratchReference | None = None
    try:
        try:
            parent = _fstat(parent_fd, ScratchPhase.BEGIN)
            if not stat.S_ISDIR(parent.st_mode):
                raise ScratchTransferError(ScratchFailureKind.UNSUPPORTED, ScratchPhase.BEGIN)
            _create_directory(parent_fd, acquisition)
            directory_fd = acquisition.directory_fd
            assert directory_fd is not None and acquisition.directory is not None
            _create_object(directory_fd, acquisition)
            object_fd = acquisition.object_fd
            assert object_fd is not None and acquisition.object is not None
            _set_mode(directory_fd, _DIRECTORY_MODE, ScratchPhase.BEGIN)
            _require_directory_stat(
                _fstat(directory_fd, ScratchPhase.BEGIN),
                acquisition.directory,
                acquisition.uid,
                acquisition.gid,
                ScratchPhase.BEGIN,
            )
            observed_object = _fstat(object_fd, ScratchPhase.BEGIN)
            _require_object_stat(
                observed_object,
                acquisition.object,
                acquisition.uid,
                acquisition.gid,
                ScratchPhase.BEGIN,
            )
            if observed_object.st_size != 0:
                raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.BEGIN)
            if _list_directory(directory_fd, ScratchPhase.BEGIN) != {_DATA_NAME}:
                raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.BEGIN)
            assert acquisition.name is not None
            result = ScratchReference(
                acquisition.name,
                acquisition.directory,
                acquisition.object,
                acquisition.uid,
                acquisition.gid,
                expected_length,
                bytes(expected_digest),
            )
        except ScratchTransferError as error:
            failure = error
        except BaseException as error:
            control = error
    finally:
        close_control = _close_descriptors(acquisition.object_fd, acquisition.directory_fd)

    if failure is None and control is None and close_control is None:
        assert result is not None
        return result

    prior = failure
    if control is None and close_control is not None:
        control = close_control
    debt = _acquisition_debt(acquisition)
    cleanup_control: BaseException | None = None
    if debt is not None:
        try:
            cleanup_failure = _cleanup_once(parent_fd, debt)
        except BaseException as error:
            cleanup_control = error
        else:
            if cleanup_failure is None:
                debt = None
    if control is None and cleanup_control is not None:
        control = cleanup_control
    if control is not None:
        _raise_control(control, prior=prior, cleanup_debt=debt)
    assert failure is not None
    raise ScratchTransferError(failure.kind, failure.phase, cleanup_debt=debt) from None


def write_scratch_chunk(
    parent_fd: int,
    reference: ScratchReference,
    offset: int,
    data: bytes,
    chunk_digest: bytes,
) -> None:
    """Write one contiguous bounded chunk, or accept an exact duplicate."""
    failure = _validate_chunk(reference, offset, data, chunk_digest)
    if failure is not None:
        _raise_with_debt(failure, reference)
    opened: _OpenedScratch | None = None
    control: BaseException | None = None
    close_control: BaseException | None = None
    try:
        opened = _open_scratch(parent_fd, reference, writable=True, phase=ScratchPhase.WRITE)
        size = opened.object_stat.st_size
        end = offset + len(data)
        if size > reference._length or offset > size or (offset < size and end > size):
            raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.WRITE)
        if offset < size:
            existing = _pread_exact(opened.object_fd, offset, len(data), ScratchPhase.WRITE)
            if not hmac.compare_digest(existing, data):
                raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.WRITE)
            observed = _fstat(opened.object_fd, ScratchPhase.WRITE)
            _require_reference_object(observed, reference, ScratchPhase.WRITE)
            if observed.st_size != size:
                raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.WRITE)
        else:
            _pwrite_all(opened.object_fd, offset, data)
            observed = _fstat(opened.object_fd, ScratchPhase.WRITE)
            _require_reference_object(observed, reference, ScratchPhase.WRITE)
            if observed.st_size != end:
                raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.WRITE)
    except ScratchTransferError as error:
        failure = error
    except BaseException as error:
        control = error
    finally:
        if opened is not None:
            close_control = opened.close()
    _finish_operation(reference, ScratchPhase.WRITE, failure, control, close_control)


def verify_scratch(parent_fd: int, reference: ScratchReference) -> ReadyScratchReference:
    """Verify the complete declared length and SHA-256 digest."""
    opened: _OpenedScratch | None = None
    failure: ScratchTransferError | None = None
    control: BaseException | None = None
    close_control: BaseException | None = None
    ready: ReadyScratchReference | None = None
    try:
        opened = _open_scratch(parent_fd, reference, writable=False, phase=ScratchPhase.VERIFY)
        before = opened.object_stat
        if before.st_size != reference._length:
            raise ScratchTransferError(ScratchFailureKind.INTEGRITY, ScratchPhase.VERIFY)
        digest = hashlib.sha256()
        offset = 0
        while offset < reference._length:
            amount = min(_HASH_READ_BYTES, reference._length - offset)
            block = _pread_exact(opened.object_fd, offset, amount, ScratchPhase.VERIFY)
            digest.update(block)
            offset += len(block)
        after = _fstat(opened.object_fd, ScratchPhase.VERIFY)
        _require_reference_object(after, reference, ScratchPhase.VERIFY)
        if not _same_verified_stat(before, after):
            raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.VERIFY)
        if not hmac.compare_digest(digest.digest(), reference._digest):
            raise ScratchTransferError(ScratchFailureKind.INTEGRITY, ScratchPhase.VERIFY)
        ready = ReadyScratchReference(reference, after.st_mtime_ns, after.st_ctime_ns)
    except ScratchTransferError as error:
        failure = error
    except BaseException as error:
        control = error
    finally:
        if opened is not None:
            close_control = opened.close()
    _finish_operation(reference, ScratchPhase.VERIFY, failure, control, close_control)
    assert ready is not None
    return ready


def read_scratch_range(parent_fd: int, ready: ReadyScratchReference, offset: int, length: int) -> bytes:
    """Read one exact bounded range from a previously verified object."""
    reference = ready._reference
    _validate_range(reference, offset, length)
    opened: _OpenedScratch | None = None
    failure: ScratchTransferError | None = None
    control: BaseException | None = None
    close_control: BaseException | None = None
    result: bytes | None = None
    try:
        opened = _open_scratch(parent_fd, reference, writable=False, phase=ScratchPhase.READ)
        _require_ready_stat(opened.object_stat, ready)
        result = _pread_exact(opened.object_fd, offset, length, ScratchPhase.READ)
        _require_ready_stat(_fstat(opened.object_fd, ScratchPhase.READ), ready)
    except ScratchTransferError as error:
        failure = error
    except BaseException as error:
        control = error
    finally:
        if opened is not None:
            close_control = opened.close()
    _finish_operation(reference, ScratchPhase.READ, failure, control, close_control)
    assert result is not None
    return result


def cleanup_scratch(
    parent_fd: int,
    owned: ScratchReference | ReadyScratchReference | ScratchCleanupDebt,
) -> None:
    """Remove only the exact verified object and its exact empty directory."""
    debt = _cleanup_debt(owned)
    failure = _cleanup_once(parent_fd, debt)
    if failure is None:
        return
    raise ScratchTransferError(failure, ScratchPhase.CLEANUP, cleanup_debt=debt) from None


def _create_directory(parent_fd: int, acquisition: _ScratchAcquisition) -> None:
    """Populate the caller-owned record after acquiring the directory.

    Pure Python cannot cover an asynchronous exception between a successful
    kernel call and assignment of its result to the acquisition record.
    """
    for _ in range(_NAME_ATTEMPTS):
        candidate = _NAME_PREFIX + secrets.token_hex(16)
        acquisition.name = candidate
        error_number: int | None = None
        try:
            os.mkdir(candidate, _DIRECTORY_MODE, dir_fd=parent_fd)
        except OSError as error:
            error_number = error.errno
        if error_number is None:
            acquisition.directory_fd = _open_directory(parent_fd, candidate, ScratchPhase.BEGIN)
            observed = _fstat(acquisition.directory_fd, ScratchPhase.BEGIN)
            acquisition.directory = _identity(observed)
            if observed.st_uid != acquisition.uid:
                raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.BEGIN)
            acquisition.gid = observed.st_gid
            return
        acquisition.name = None
        if error_number != errno.EEXIST:
            raise ScratchTransferError(ScratchFailureKind.IO, ScratchPhase.BEGIN)
    raise ScratchTransferError(ScratchFailureKind.IO, ScratchPhase.BEGIN)


def _create_object(directory_fd: int, acquisition: _ScratchAcquisition) -> None:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    error_number: int | None = None
    try:
        acquisition.object_fd = os.open(_DATA_NAME, flags, _OBJECT_MODE, dir_fd=directory_fd)
    except OSError as error:
        error_number = error.errno
    if error_number is not None:
        kind = ScratchFailureKind.CONFLICT if error_number == errno.EEXIST else ScratchFailureKind.IO
        raise ScratchTransferError(kind, ScratchPhase.BEGIN)
    assert acquisition.object_fd is not None
    observed = _fstat(acquisition.object_fd, ScratchPhase.BEGIN)
    acquisition.object = _identity(observed)
    _set_mode(acquisition.object_fd, _OBJECT_MODE, ScratchPhase.BEGIN)


def _open_scratch(
    parent_fd: int,
    reference: ScratchReference,
    *,
    writable: bool,
    phase: ScratchPhase,
) -> _OpenedScratch:
    directory_stat = _stat_at(parent_fd, reference._name, phase)
    if directory_stat is None:
        raise ScratchTransferError(ScratchFailureKind.CONFLICT, phase)
    _require_reference_directory(directory_stat, reference, phase)
    directory_fd = _open_directory(parent_fd, reference._name, phase)
    try:
        _require_reference_directory(_fstat(directory_fd, phase), reference, phase)
        object_stat = _stat_at(directory_fd, _DATA_NAME, phase)
        if object_stat is None:
            raise ScratchTransferError(ScratchFailureKind.CONFLICT, phase)
        _require_reference_object(object_stat, reference, phase)
        object_fd = _open_object(directory_fd, writable, phase)
        try:
            opened_stat = _fstat(object_fd, phase)
            _require_reference_object(opened_stat, reference, phase)
            return _OpenedScratch(directory_fd, object_fd, opened_stat)
        except BaseException:
            with suppress(OSError):
                os.close(object_fd)
            raise
    except BaseException:
        with suppress(OSError):
            os.close(directory_fd)
        raise


def _open_directory(parent_fd: int, name: str, phase: ScratchPhase) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    return _open_at(parent_fd, name, flags, phase)


def _open_object(directory_fd: int, writable: bool, phase: ScratchPhase) -> int:
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    return _open_at(directory_fd, _DATA_NAME, flags, phase)


def _open_at(parent_fd: int, name: str, flags: int, phase: ScratchPhase) -> int:
    error_number: int | None = None
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        error_number = error.errno
        descriptor = -1
    if error_number is None:
        return descriptor
    if error_number in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO, errno.ENODEV}:
        raise ScratchTransferError(ScratchFailureKind.UNSUPPORTED, phase)
    if error_number == errno.ENOENT:
        raise ScratchTransferError(ScratchFailureKind.CONFLICT, phase)
    raise ScratchTransferError(ScratchFailureKind.IO, phase)


def _cleanup_once(parent_fd: int, debt: ScratchCleanupDebt) -> ScratchFailureKind | None:
    stat_ok, directory_stat = _try_stat_at(parent_fd, debt._name)
    if not stat_ok:
        return ScratchFailureKind.IO
    if directory_stat is None:
        return None
    if debt._directory is None or not _matches_directory(directory_stat, debt._directory, debt._uid, debt._gid):
        return ScratchFailureKind.CONFLICT
    directory_fd: int | None = None
    try:
        directory_fd = _open_directory(parent_fd, debt._name, ScratchPhase.CLEANUP)
        if not _matches_directory(
            _fstat(directory_fd, ScratchPhase.CLEANUP),
            debt._directory,
            debt._uid,
            debt._gid,
        ):
            return ScratchFailureKind.CONFLICT
        names = _list_directory(directory_fd, ScratchPhase.CLEANUP)
        if names - {_DATA_NAME}:
            return ScratchFailureKind.CONFLICT
        if _DATA_NAME in names:
            object_ok, object_stat = _try_stat_at(directory_fd, _DATA_NAME)
            if not object_ok:
                return ScratchFailureKind.IO
            if (
                debt._object is None
                or object_stat is None
                or not _matches_object(
                    object_stat,
                    debt._object,
                    debt._uid,
                    debt._gid,
                )
            ):
                return ScratchFailureKind.CONFLICT
            try:
                os.unlink(_DATA_NAME, dir_fd=directory_fd)
            except OSError:
                return ScratchFailureKind.IO
        if _list_directory(directory_fd, ScratchPhase.CLEANUP):
            return ScratchFailureKind.CONFLICT
    except ScratchTransferError as error:
        return error.kind
    finally:
        if directory_fd is not None:
            with suppress(OSError):
                os.close(directory_fd)
    final_ok, final_stat = _try_stat_at(parent_fd, debt._name)
    if not final_ok:
        return ScratchFailureKind.IO
    if final_stat is None:
        return None
    if not _matches_directory(final_stat, debt._directory, debt._uid, debt._gid):
        return ScratchFailureKind.CONFLICT
    try:
        os.rmdir(debt._name, dir_fd=parent_fd)
    except OSError:
        return ScratchFailureKind.IO
    return None


def _require_reference_directory(
    observed: os.stat_result,
    reference: ScratchReference,
    phase: ScratchPhase,
) -> None:
    _require_directory_stat(observed, reference._directory, reference._uid, reference._gid, phase)


def _require_reference_object(
    observed: os.stat_result,
    reference: ScratchReference,
    phase: ScratchPhase,
) -> None:
    _require_object_stat(observed, reference._object, reference._uid, reference._gid, phase)


def _require_directory_stat(
    observed: os.stat_result,
    identity: _Identity,
    uid: int,
    gid: int,
    phase: ScratchPhase,
) -> None:
    if not stat.S_ISDIR(observed.st_mode):
        raise ScratchTransferError(ScratchFailureKind.UNSUPPORTED, phase)
    if not _matches_directory(observed, identity, uid, gid):
        raise ScratchTransferError(ScratchFailureKind.CONFLICT, phase)


def _require_object_stat(
    observed: os.stat_result,
    identity: _Identity,
    uid: int,
    gid: int,
    phase: ScratchPhase,
) -> None:
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ScratchTransferError(ScratchFailureKind.UNSUPPORTED, phase)
    if not _matches_object(observed, identity, uid, gid):
        raise ScratchTransferError(ScratchFailureKind.CONFLICT, phase)


def _matches_directory(observed: os.stat_result, identity: _Identity, uid: int, gid: int) -> bool:
    return (
        stat.S_ISDIR(observed.st_mode)
        and _identity(observed) == identity
        and observed.st_uid == uid
        and observed.st_gid == gid
        and stat.S_IMODE(observed.st_mode) == _DIRECTORY_MODE
    )


def _matches_object(observed: os.stat_result, identity: _Identity, uid: int, gid: int) -> bool:
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_nlink == 1
        and _identity(observed) == identity
        and observed.st_uid == uid
        and observed.st_gid == gid
        and stat.S_IMODE(observed.st_mode) == _OBJECT_MODE
    )


def _require_ready_stat(observed: os.stat_result, ready: ReadyScratchReference) -> None:
    reference = ready._reference
    _require_reference_object(observed, reference, ScratchPhase.READ)
    if (
        observed.st_size != reference._length
        or observed.st_mtime_ns != ready._modified_ns
        or observed.st_ctime_ns != ready._changed_ns
    ):
        raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.READ)


def _same_verified_stat(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )


def _validate_contract(expected_length: int, expected_digest: bytes) -> None:
    if expected_length < 0 or expected_length > _MAX_OFFSET:
        raise ValueError("Expected scratch length is outside the supported range")
    if len(expected_digest) != hashlib.sha256().digest_size:
        raise ValueError("Expected scratch digest must be a SHA-256 digest")


def _validate_chunk(
    reference: ScratchReference,
    offset: int,
    data: bytes,
    chunk_digest: bytes,
) -> ScratchTransferError | None:
    if offset < 0 or offset > _MAX_OFFSET:
        return ScratchTransferError(ScratchFailureKind.LIMIT, ScratchPhase.WRITE)
    if not data or len(data) > _MAX_CHUNK_BYTES:
        return ScratchTransferError(ScratchFailureKind.LIMIT, ScratchPhase.WRITE)
    if offset > reference._length or len(data) > reference._length - offset:
        return ScratchTransferError(ScratchFailureKind.LIMIT, ScratchPhase.WRITE)
    if not hmac.compare_digest(hashlib.sha256(data).digest(), chunk_digest):
        return ScratchTransferError(ScratchFailureKind.INTEGRITY, ScratchPhase.WRITE)
    return None


def _validate_range(reference: ScratchReference, offset: int, length: int) -> None:
    failure = None
    if (
        offset < 0
        or offset > _MAX_OFFSET
        or length < 0
        or length > _MAX_CHUNK_BYTES
        or offset > reference._length
        or length > reference._length - offset
    ):
        failure = ScratchTransferError(ScratchFailureKind.LIMIT, ScratchPhase.READ)
    if failure is not None:
        _raise_with_debt(failure, reference)


def _pwrite_all(descriptor: int, offset: int, data: bytes) -> None:
    position = 0
    failure = False
    while position < len(data):
        try:
            written = os.pwrite(descriptor, data[position:], offset + position)
        except OSError:
            failure = True
            break
        if written <= 0:
            failure = True
            break
        position += written
    if failure:
        raise ScratchTransferError(ScratchFailureKind.IO, ScratchPhase.WRITE)


def _pread_exact(descriptor: int, offset: int, length: int, phase: ScratchPhase) -> bytes:
    result = bytearray()
    failure: ScratchFailureKind | None = None
    while len(result) < length:
        try:
            block = os.pread(descriptor, length - len(result), offset + len(result))
        except OSError:
            failure = ScratchFailureKind.IO
            break
        if not block:
            failure = ScratchFailureKind.CONFLICT
            break
        result.extend(block)
    if failure is not None:
        raise ScratchTransferError(failure, phase)
    return bytes(result)


def _set_mode(descriptor: int, mode: int, phase: ScratchPhase) -> None:
    failed = False
    try:
        os.fchmod(descriptor, mode)
    except OSError:
        failed = True
    if failed:
        raise ScratchTransferError(ScratchFailureKind.IO, phase)


def _fstat(descriptor: int, phase: ScratchPhase) -> os.stat_result:
    failed = False
    result: os.stat_result | None = None
    try:
        result = os.fstat(descriptor)
    except OSError:
        failed = True
    if failed or result is None:
        raise ScratchTransferError(ScratchFailureKind.IO, phase)
    return result


def _stat_at(parent_fd: int, name: str, phase: ScratchPhase) -> os.stat_result | None:
    error_number: int | None = None
    try:
        result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        error_number = error.errno
        result = None
    if error_number == errno.ENOENT:
        return None
    if error_number is not None or result is None:
        raise ScratchTransferError(ScratchFailureKind.IO, phase)
    return result


def _try_stat_at(parent_fd: int, name: str) -> tuple[bool, os.stat_result | None]:
    try:
        return True, os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True, None
    except OSError:
        return False, None


def _list_directory(directory_fd: int, phase: ScratchPhase) -> set[str]:
    failed = False
    names: list[str] = []
    try:
        names = os.listdir(directory_fd)
    except OSError:
        failed = True
    if failed:
        raise ScratchTransferError(ScratchFailureKind.IO, phase)
    return set(names)


def _identity(observed: os.stat_result) -> _Identity:
    return _Identity(observed.st_dev, observed.st_ino)


def _acquisition_debt(acquisition: _ScratchAcquisition) -> ScratchCleanupDebt | None:
    if acquisition.name is None:
        return None
    return ScratchCleanupDebt(
        acquisition.name,
        acquisition.directory,
        acquisition.object,
        acquisition.uid,
        acquisition.gid,
    )


def _close_descriptors(*descriptors: int | None) -> BaseException | None:
    interrupted: BaseException | None = None
    for descriptor in descriptors:
        if descriptor is None:
            continue
        try:
            _close_fd(descriptor)
        except BaseException as control:
            if interrupted is None:
                interrupted = control
    return interrupted


def _close_fd(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)


def _raise_control(
    control: BaseException,
    *,
    prior: ScratchTransferError | None,
    cleanup_debt: ScratchCleanupDebt | None,
) -> NoReturn:
    fact = prior
    if cleanup_debt is not None:
        kind = ScratchFailureKind.IO if fact is None else fact.kind
        phase = ScratchPhase.BEGIN if fact is None else fact.phase
        fact = ScratchTransferError(kind, phase, cleanup_debt=cleanup_debt)
    if fact is None:
        raise control
    raise control from fact


def _finish_operation(
    reference: ScratchReference,
    phase: ScratchPhase,
    failure: ScratchTransferError | None,
    control: BaseException | None,
    close_control: BaseException | None,
) -> None:
    if control is None:
        control = close_control
    if control is not None:
        prior = failure or ScratchTransferError(ScratchFailureKind.IO, phase)
        _raise_control(control, prior=prior, cleanup_debt=_cleanup_debt(reference))
    if failure is not None:
        _raise_with_debt(failure, reference)


def _cleanup_debt(owned: ScratchReference | ReadyScratchReference | ScratchCleanupDebt) -> ScratchCleanupDebt:
    if isinstance(owned, ReadyScratchReference):
        owned = owned._reference
    if isinstance(owned, ScratchReference):
        return ScratchCleanupDebt(
            owned._name,
            owned._directory,
            owned._object,
            owned._uid,
            owned._gid,
        )
    return owned


def _raise_with_debt(error: ScratchTransferError, reference: ScratchReference) -> NoReturn:
    raise ScratchTransferError(
        error.kind,
        error.phase,
        cleanup_debt=_cleanup_debt(reference),
    ) from None
