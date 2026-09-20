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
import stat
import time
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, NoReturn

from ._scratch_receipt import (
    _DATA_MODE as _RECEIPT_DATA_MODE,
)
from ._scratch_receipt import (
    _DATA_NAME as _RECEIPT_DATA_NAME,
)
from ._scratch_receipt import (
    _DIRECTORY_MODE,
    _RECEIPT_BUILD_MODE,
    _RECEIPT_MODE,
    _RECEIPT_NAME,
    ScratchCleanupDebt,
    ScratchHistoricalOwnership,
    ScratchOwnership,
    ScratchOwnershipUncertainty,
    ScratchReceiptAcquisition,
    ScratchReceiptContext,
    ScratchReceiptError,
    ScratchReceiptFailureKind,
    _Identity,
    cleanup_owned_scratch,
    create_receipt,
    matches_receipt_context,
    matches_scratch_data,
    matches_scratch_directory,
    validate_receipt,
)
from ._scratch_receipt import (
    reconcile_scratch_ownership as _reconcile_receipt_ownership,
)
from ._scratch_receipt import (
    scratch_name as _scratch_name,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_DATA_NAME = _RECEIPT_DATA_NAME
_OBJECT_MODE = _RECEIPT_DATA_MODE
scratch_name = _scratch_name

# Internal candidate pending whole-request carrier proof.
_MAX_CHUNK_BYTES = 24 * 1024
_MAX_OFFSET = (1 << 63) - 1
_HASH_READ_BYTES = 64 * 1024


class ScratchFailureKind(Enum):
    """Closed failures that reveal no transferred bytes or scratch names."""

    UNSUPPORTED = "unsupported"
    CONFLICT = "conflict"
    LIMIT = "limit"
    INTEGRITY = "integrity"
    DEADLINE = "deadline"
    IO = "io"


class ScratchPhase(Enum):
    """The fixed scratch operation in which work stopped."""

    BEGIN = "begin"
    WRITE = "write"
    VERIFY = "verify"
    READ = "read"
    RECONCILE = "reconcile"
    CLEANUP = "cleanup"


@dataclass(frozen=True, repr=False)
class ScratchReference:
    """Identity and length contract for one private scratch object."""

    _ownership: ScratchOwnership


@dataclass(frozen=True, repr=False)
class ReadyScratchReference:
    """A scratch reference whose whole object was verified successfully."""

    _reference: ScratchReference
    _digest: bytes
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
    parent: _Identity | None = None
    directory_fd: int | None = None
    object_fd: int | None = None
    directory: _Identity | None = None
    object: _Identity | None = None
    receipt: ScratchReceiptAcquisition = field(default_factory=ScratchReceiptAcquisition)


@dataclass(repr=False)
class _OpenedScratch:
    directory_fd: int
    object_fd: int
    object_stat: os.stat_result

    def close(self) -> BaseException | None:
        return _close_descriptors(self.object_fd, self.directory_fd)


def begin_scratch(
    parent_fd: int,
    expected_length: int,
    token: bytes,
    context: ScratchReceiptContext,
    *,
    expires_at: float | None = None,
) -> ScratchReference:
    """Create one private directory and fixed data object beneath ``parent_fd``."""
    _validate_contract(expected_length)
    name = scratch_name(token)
    if not isinstance(context, ScratchReceiptContext):
        raise ValueError("Scratch receipt context has an invalid type")
    if not matches_receipt_context(context):
        raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.BEGIN)
    _check_deadline(expires_at, ScratchPhase.BEGIN)
    acquisition = _ScratchAcquisition(context.identity.euid, context.identity.egid)
    failure: ScratchTransferError | None = None
    control: BaseException | None = None
    close_control: BaseException | None = None
    result: ScratchReference | None = None
    try:
        try:
            parent = _fstat(parent_fd, ScratchPhase.BEGIN)
            if not stat.S_ISDIR(parent.st_mode):
                raise ScratchTransferError(ScratchFailureKind.UNSUPPORTED, ScratchPhase.BEGIN)
            acquisition.parent = _identity(parent)
            _create_directory(parent_fd, name, acquisition, expires_at)
            directory_fd = acquisition.directory_fd
            assert directory_fd is not None and acquisition.directory is not None
            try:
                _check_deadline(expires_at, ScratchPhase.BEGIN)
            except ScratchTransferError:
                # This does not advance acquisition after expiry. It only
                # restores the exact mode required by bounded cleanup.
                with suppress(ScratchTransferError):
                    _set_mode(directory_fd, _DIRECTORY_MODE, ScratchPhase.BEGIN)
                raise
            _create_object(directory_fd, acquisition)
            object_fd = acquisition.object_fd
            assert object_fd is not None and acquisition.object is not None
            assert acquisition.parent is not None
            assert acquisition.directory is not None
            assert acquisition.object is not None
            try:
                _check_deadline(expires_at, ScratchPhase.BEGIN)
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
                _check_deadline(expires_at, ScratchPhase.BEGIN)
                ownership = _create_receipt(
                    directory_fd,
                    bytes(token),
                    context,
                    acquisition.parent,
                    acquisition.directory,
                    acquisition.object,
                    acquisition.gid,
                    expected_length,
                    acquisition.receipt,
                    expires_at,
                )
            except BaseException:
                # Both fixed objects inherit a setgid parent's group before
                # cleanup-only normalization removes the inherited bit.
                with suppress(ScratchTransferError):
                    _set_mode(directory_fd, _DIRECTORY_MODE, ScratchPhase.BEGIN)
                raise
            _set_mode(directory_fd, _DIRECTORY_MODE, ScratchPhase.BEGIN)
            _check_deadline(expires_at, ScratchPhase.BEGIN)
            _require_directory_stat(
                _fstat(directory_fd, ScratchPhase.BEGIN),
                acquisition.directory,
                acquisition.uid,
                acquisition.gid,
                ScratchPhase.BEGIN,
            )
            _require_object_stat(
                _fstat(object_fd, ScratchPhase.BEGIN),
                acquisition.object,
                acquisition.uid,
                acquisition.gid,
                ScratchPhase.BEGIN,
            )
            if _list_directory(directory_fd, ScratchPhase.BEGIN) != {_DATA_NAME, _RECEIPT_NAME}:
                raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.BEGIN)
            _check_deadline(expires_at, ScratchPhase.BEGIN)
            result = ScratchReference(ownership)
        except ScratchTransferError as error:
            failure = error
        except BaseException as error:
            control = error
    finally:
        close_control = _close_descriptors(acquisition.object_fd, acquisition.directory_fd)

    if failure is None and control is None and close_control is None:
        try:
            _check_deadline(expires_at, ScratchPhase.BEGIN)
        except ScratchTransferError as error:
            failure = error
        else:
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
    *,
    expires_at: float | None = None,
) -> None:
    """Write one contiguous bounded chunk, or accept an exact duplicate."""
    failure = _validate_chunk(reference, offset, data, chunk_digest)
    if failure is not None:
        _raise_with_debt(failure, reference)
    _check_reference_deadline(reference, expires_at, ScratchPhase.WRITE)
    opened: _OpenedScratch | None = None
    control: BaseException | None = None
    close_control: BaseException | None = None
    try:
        opened = _open_scratch(
            parent_fd,
            reference,
            writable=True,
            phase=ScratchPhase.WRITE,
            expires_at=expires_at,
        )
        ownership = reference._ownership
        size = opened.object_stat.st_size
        end = offset + len(data)
        if size > ownership._length or offset > size or (offset < size and end > size):
            raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.WRITE)
        if offset < size:
            existing = _pread_exact(
                opened.object_fd,
                offset,
                len(data),
                ScratchPhase.WRITE,
                expires_at,
            )
            if not hmac.compare_digest(existing, data):
                raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.WRITE)
            observed = _fstat(opened.object_fd, ScratchPhase.WRITE)
            _require_reference_object(observed, reference, ScratchPhase.WRITE)
            if observed.st_size != size:
                raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.WRITE)
        else:
            _pwrite_all(opened.object_fd, offset, data, expires_at)
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
    _finish_operation(reference, ScratchPhase.WRITE, failure, control, close_control, expires_at)


def reconcile_scratch_ownership(
    parent_fd: int,
    token: bytes,
    context: ScratchReceiptContext,
    *,
    expires_at: float | None = None,
) -> ScratchHistoricalOwnership | ScratchOwnershipUncertainty:
    """Read-only recovery of receipt-backed ownership for exact cleanup."""
    try:
        return _reconcile_receipt_ownership(parent_fd, token, context, expires_at=expires_at)
    except ScratchReceiptError as error:
        raise ScratchTransferError(_map_receipt_failure(error.kind), ScratchPhase.RECONCILE) from None


def verify_scratch(
    parent_fd: int,
    reference: ScratchReference,
    expected_digest: bytes,
    *,
    expires_at: float | None = None,
) -> ReadyScratchReference:
    """Verify the complete declared length and SHA-256 digest."""
    _validate_digest(expected_digest)
    _check_reference_deadline(reference, expires_at, ScratchPhase.VERIFY)
    opened: _OpenedScratch | None = None
    failure: ScratchTransferError | None = None
    control: BaseException | None = None
    close_control: BaseException | None = None
    ready: ReadyScratchReference | None = None
    try:
        opened = _open_scratch(
            parent_fd,
            reference,
            writable=False,
            phase=ScratchPhase.VERIFY,
            expires_at=expires_at,
        )
        ownership = reference._ownership
        before = opened.object_stat
        if before.st_size != ownership._length:
            raise ScratchTransferError(ScratchFailureKind.INTEGRITY, ScratchPhase.VERIFY)
        digest = hashlib.sha256()
        offset = 0
        while offset < ownership._length:
            _check_deadline(expires_at, ScratchPhase.VERIFY)
            amount = min(_HASH_READ_BYTES, ownership._length - offset)
            block = _pread_exact(
                opened.object_fd,
                offset,
                amount,
                ScratchPhase.VERIFY,
                expires_at,
            )
            digest.update(block)
            offset += len(block)
        after = _fstat(opened.object_fd, ScratchPhase.VERIFY)
        _require_reference_object(after, reference, ScratchPhase.VERIFY)
        if not _same_verified_stat(before, after):
            raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.VERIFY)
        if not hmac.compare_digest(digest.digest(), expected_digest):
            raise ScratchTransferError(ScratchFailureKind.INTEGRITY, ScratchPhase.VERIFY)
        ready = ReadyScratchReference(reference, bytes(expected_digest), after.st_mtime_ns, after.st_ctime_ns)
    except ScratchTransferError as error:
        failure = error
    except BaseException as error:
        control = error
    finally:
        if opened is not None:
            close_control = opened.close()
    _finish_operation(reference, ScratchPhase.VERIFY, failure, control, close_control, expires_at)
    assert ready is not None
    return ready


def read_scratch_range(
    parent_fd: int,
    ready: ReadyScratchReference,
    offset: int,
    length: int,
    *,
    expires_at: float | None = None,
) -> bytes:
    """Read one exact bounded range from a previously verified object."""
    reference = ready._reference
    _validate_range(reference, offset, length)
    _check_reference_deadline(reference, expires_at, ScratchPhase.READ)
    opened: _OpenedScratch | None = None
    failure: ScratchTransferError | None = None
    control: BaseException | None = None
    close_control: BaseException | None = None
    result: bytes | None = None
    try:
        opened = _open_scratch(
            parent_fd,
            reference,
            writable=False,
            phase=ScratchPhase.READ,
            expires_at=expires_at,
        )
        _require_ready_stat(opened.object_stat, ready)
        result = _pread_exact(opened.object_fd, offset, length, ScratchPhase.READ, expires_at)
        _require_ready_stat(_fstat(opened.object_fd, ScratchPhase.READ), ready)
    except ScratchTransferError as error:
        failure = error
    except BaseException as error:
        control = error
    finally:
        if opened is not None:
            close_control = opened.close()
    _finish_operation(reference, ScratchPhase.READ, failure, control, close_control, expires_at)
    assert result is not None
    return result


def ready_scratch_contract(ready: ReadyScratchReference) -> tuple[int, bytes]:
    """Return the verified object's declared length and digest."""
    if not isinstance(ready, ReadyScratchReference):
        raise ValueError("Ready scratch reference has an invalid type")
    reference = ready._reference
    return reference._ownership._length, bytes(ready._digest)


def iter_ready_scratch(
    parent_fd: int,
    ready: ReadyScratchReference,
    *,
    expires_at: float | None = None,
) -> Iterator[bytes]:
    """Yield verified scratch content in the transfer substrate's bounded ranges."""
    length, _ = ready_scratch_contract(ready)
    if length == 0:
        read_scratch_range(parent_fd, ready, 0, 0, expires_at=expires_at)
        return
    offset = 0
    while offset < length:
        block = read_scratch_range(
            parent_fd,
            ready,
            offset,
            min(_MAX_CHUNK_BYTES, length - offset),
            expires_at=expires_at,
        )
        yield block
        offset += len(block)
    _check_reference_deadline(ready._reference, expires_at, ScratchPhase.READ)


def cleanup_scratch(
    parent_fd: int,
    owned: ScratchReference | ReadyScratchReference | ScratchHistoricalOwnership | ScratchCleanupDebt,
) -> None:
    """Remove only the exact verified object and its exact empty directory."""
    debt = _cleanup_debt(owned)
    try:
        failure = _cleanup_once(parent_fd, debt)
    except BaseException as control:
        _raise_control(
            control,
            prior=ScratchTransferError(ScratchFailureKind.IO, ScratchPhase.CLEANUP),
            cleanup_debt=debt,
        )
    if failure is None:
        return
    raise ScratchTransferError(failure, ScratchPhase.CLEANUP, cleanup_debt=debt) from None


def _create_directory(
    parent_fd: int,
    name: str,
    acquisition: _ScratchAcquisition,
    expires_at: float | None,
) -> None:
    """Populate the caller-owned record after acquiring the directory.

    Pure Python cannot cover an asynchronous exception between a successful
    kernel call and assignment of its result to the acquisition record.
    """
    _check_deadline(expires_at, ScratchPhase.BEGIN)
    try:
        os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_fd)
    except OSError as error:
        kind = ScratchFailureKind.CONFLICT if error.errno == errno.EEXIST else ScratchFailureKind.IO
        raise ScratchTransferError(kind, ScratchPhase.BEGIN) from None
    acquisition.name = name
    acquisition.directory_fd = _open_directory(parent_fd, name, ScratchPhase.BEGIN)
    observed = _fstat(acquisition.directory_fd, ScratchPhase.BEGIN)
    acquisition.directory = _identity(observed)
    if observed.st_uid != acquisition.uid:
        raise ScratchTransferError(ScratchFailureKind.CONFLICT, ScratchPhase.BEGIN)
    acquisition.gid = observed.st_gid


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


def _create_receipt(
    directory_fd: int,
    token: bytes,
    context: ScratchReceiptContext,
    parent: _Identity,
    directory: _Identity,
    data: _Identity,
    gid: int,
    length: int,
    acquisition: ScratchReceiptAcquisition,
    expires_at: float | None,
) -> ScratchOwnership:
    try:
        return create_receipt(
            directory_fd,
            token,
            context,
            parent,
            directory,
            data,
            gid,
            length,
            acquisition,
            expires_at=expires_at,
        )
    except ScratchReceiptError as error:
        raise ScratchTransferError(_map_receipt_failure(error.kind), ScratchPhase.BEGIN) from None


def _validate_receipt(
    directory_fd: int,
    ownership: ScratchOwnership,
    phase: ScratchPhase,
    expires_at: float | None,
) -> None:
    try:
        validate_receipt(directory_fd, ownership, expires_at=expires_at)
    except ScratchReceiptError as error:
        raise ScratchTransferError(_map_receipt_failure(error.kind), phase) from None


def _map_receipt_failure(kind: ScratchReceiptFailureKind) -> ScratchFailureKind:
    return {
        ScratchReceiptFailureKind.UNSUPPORTED: ScratchFailureKind.UNSUPPORTED,
        ScratchReceiptFailureKind.CONFLICT: ScratchFailureKind.CONFLICT,
        ScratchReceiptFailureKind.DEADLINE: ScratchFailureKind.DEADLINE,
        ScratchReceiptFailureKind.IO: ScratchFailureKind.IO,
    }[kind]


def _open_scratch(
    parent_fd: int,
    reference: ScratchReference,
    *,
    writable: bool,
    phase: ScratchPhase,
    expires_at: float | None,
) -> _OpenedScratch:
    ownership = reference._ownership
    name = scratch_name(ownership._token)
    parent = _fstat(parent_fd, phase)
    if _identity(parent) != ownership._parent:
        raise ScratchTransferError(ScratchFailureKind.CONFLICT, phase)
    directory_stat = _stat_at(parent_fd, name, phase)
    if directory_stat is None:
        raise ScratchTransferError(ScratchFailureKind.CONFLICT, phase)
    _require_reference_directory(directory_stat, reference, phase)
    directory_fd = _open_directory(parent_fd, name, phase)
    try:
        _require_reference_directory(_fstat(directory_fd, phase), reference, phase)
        _validate_receipt(directory_fd, reference._ownership, phase, expires_at)
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
    result = cleanup_owned_scratch(parent_fd, debt)
    return None if result is None else _map_receipt_failure(result)


def _require_reference_directory(
    observed: os.stat_result,
    reference: ScratchReference,
    phase: ScratchPhase,
) -> None:
    ownership = reference._ownership
    _require_directory_stat(
        observed,
        ownership._directory,
        ownership._context.identity.euid,
        ownership._gid,
        phase,
    )


def _require_reference_object(
    observed: os.stat_result,
    reference: ScratchReference,
    phase: ScratchPhase,
) -> None:
    ownership = reference._ownership
    _require_object_stat(
        observed,
        ownership._data,
        ownership._context.identity.euid,
        ownership._gid,
        phase,
    )


def _require_directory_stat(
    observed: os.stat_result,
    identity: _Identity,
    uid: int,
    gid: int,
    phase: ScratchPhase,
) -> None:
    if not stat.S_ISDIR(observed.st_mode):
        raise ScratchTransferError(ScratchFailureKind.UNSUPPORTED, phase)
    if not matches_scratch_directory(observed, identity, uid, gid):
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
    if not matches_scratch_data(observed, identity, uid, gid):
        raise ScratchTransferError(ScratchFailureKind.CONFLICT, phase)


def _require_ready_stat(observed: os.stat_result, ready: ReadyScratchReference) -> None:
    reference = ready._reference
    ownership = reference._ownership
    _require_reference_object(observed, reference, ScratchPhase.READ)
    if (
        observed.st_size != ownership._length
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


def _validate_contract(expected_length: int) -> None:
    if expected_length < 0 or expected_length > _MAX_OFFSET:
        raise ValueError("Expected scratch length is outside the supported range")


def _validate_digest(expected_digest: bytes) -> None:
    if len(expected_digest) != hashlib.sha256().digest_size:
        raise ValueError("Expected scratch digest must be a SHA-256 digest")


def _validate_chunk(
    reference: ScratchReference,
    offset: int,
    data: bytes,
    chunk_digest: bytes,
) -> ScratchTransferError | None:
    length = reference._ownership._length
    if offset < 0 or offset > _MAX_OFFSET:
        return ScratchTransferError(ScratchFailureKind.LIMIT, ScratchPhase.WRITE)
    if not data or len(data) > _MAX_CHUNK_BYTES:
        return ScratchTransferError(ScratchFailureKind.LIMIT, ScratchPhase.WRITE)
    if offset > length or len(data) > length - offset:
        return ScratchTransferError(ScratchFailureKind.LIMIT, ScratchPhase.WRITE)
    if not hmac.compare_digest(hashlib.sha256(data).digest(), chunk_digest):
        return ScratchTransferError(ScratchFailureKind.INTEGRITY, ScratchPhase.WRITE)
    return None


def _validate_range(reference: ScratchReference, offset: int, length: int) -> None:
    expected_length = reference._ownership._length
    failure = None
    if (
        offset < 0
        or offset > _MAX_OFFSET
        or length < 0
        or length > _MAX_CHUNK_BYTES
        or offset > expected_length
        or length > expected_length - offset
    ):
        failure = ScratchTransferError(ScratchFailureKind.LIMIT, ScratchPhase.READ)
    if failure is not None:
        _raise_with_debt(failure, reference)


def _pwrite_all(descriptor: int, offset: int, data: bytes, expires_at: float | None) -> None:
    position = 0
    failure = False
    while position < len(data):
        _check_deadline(expires_at, ScratchPhase.WRITE)
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
    _check_deadline(expires_at, ScratchPhase.WRITE)


def _pread_exact(
    descriptor: int,
    offset: int,
    length: int,
    phase: ScratchPhase,
    expires_at: float | None,
) -> bytes:
    result = bytearray()
    failure: ScratchFailureKind | None = None
    while len(result) < length:
        _check_deadline(expires_at, phase)
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
    _check_deadline(expires_at, phase)
    return bytes(result)


def _check_deadline(expires_at: float | None, phase: ScratchPhase) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise ScratchTransferError(ScratchFailureKind.DEADLINE, phase)


def _check_reference_deadline(
    reference: ScratchReference,
    expires_at: float | None,
    phase: ScratchPhase,
) -> None:
    try:
        _check_deadline(expires_at, phase)
    except ScratchTransferError as error:
        _raise_with_debt(error, reference)


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
        acquisition.parent,
        acquisition.directory,
        acquisition.object,
        acquisition.receipt.identity,
        (_RECEIPT_BUILD_MODE, _RECEIPT_MODE),
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
    expires_at: float | None,
) -> None:
    if control is None:
        control = close_control
    if control is not None:
        prior = failure or ScratchTransferError(ScratchFailureKind.IO, phase)
        _raise_control(control, prior=prior, cleanup_debt=_cleanup_debt(reference))
    if failure is None:
        try:
            _check_deadline(expires_at, phase)
        except ScratchTransferError as error:
            failure = error
    if failure is not None:
        _raise_with_debt(failure, reference)


def _cleanup_debt(
    owned: ScratchReference | ReadyScratchReference | ScratchHistoricalOwnership | ScratchCleanupDebt,
) -> ScratchCleanupDebt:
    if isinstance(owned, ReadyScratchReference):
        owned = owned._reference
    if isinstance(owned, ScratchHistoricalOwnership):
        ownership = owned._ownership
        return _ownership_debt(ownership)
    if isinstance(owned, ScratchReference):
        return _ownership_debt(owned._ownership)
    return owned


def _ownership_debt(ownership: ScratchOwnership) -> ScratchCleanupDebt:
    return ScratchCleanupDebt(
        scratch_name(ownership._token),
        ownership._parent,
        ownership._directory,
        ownership._data,
        ownership._receipt,
        (_RECEIPT_MODE,),
        ownership._context.identity.euid,
        ownership._gid,
    )


def _raise_with_debt(error: ScratchTransferError, reference: ScratchReference) -> NoReturn:
    raise ScratchTransferError(
        error.kind,
        error.phase,
        cleanup_debt=_cleanup_debt(reference),
    ) from None
