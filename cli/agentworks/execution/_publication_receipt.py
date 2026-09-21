"""Immutable ownership receipts for recoverable publication stages."""

from __future__ import annotations

import errno
import hmac
import json
import os
import stat
import time
from dataclasses import dataclass
from enum import Enum

from . import _scratch
from ._scratch import ReadyScratchReference, ScratchFailureKind, ScratchPhase, ScratchReference, ScratchTransferError
from ._scratch_receipt import (
    ScratchOperation,
    ScratchOwnership,
    ScratchReceiptError,
    ScratchReceiptFailureKind,
    matches_receipt_context,
    matches_scratch_directory,
    scratch_name,
    validate_receipt,
)

_RECORD_NAME = "publication-receipt"
_RECORD_BUILD_MODE = 0o600
_RECORD_MODE = 0o400
_STAGE_PREFIX = ".agentworks-stage-"
_MAX_RECORD_BYTES = 1024
_MAX_IDENTITY_NUMBER = (1 << 64) - 1


class PublicationReceiptFailureKind(Enum):
    """Closed failures from publication ownership bookkeeping."""

    UNSUPPORTED = "unsupported"
    CONFLICT = "conflict"
    DEADLINE = "deadline"
    IO = "io"


@dataclass(frozen=True, repr=False)
class _Identity:
    device: int
    inode: int


@dataclass(repr=False)
class PublicationStageAdmission:
    """Held, validated scratch state preceding destination stage creation."""

    _opened: _scratch._OpenedScratch
    _scratch_ownership: ScratchOwnership
    _publication_parent: _Identity
    _stage_name: str
    _closed: bool = False

    @property
    def stage_name(self) -> str:
        """Return the sole token-derived destination stage name."""
        return self._stage_name

    def close(self) -> BaseException | None:
        """Close the held scratch descriptors exactly once."""
        if self._closed:
            return None
        self._closed = True
        return self._opened.close()


@dataclass(frozen=True, repr=False)
class PublicationStageOwnership:
    """Active exact stage and immutable record ownership."""

    _scratch_ownership: ScratchOwnership
    _publication_parent: _Identity
    _stage_name: str
    _stage: _Identity
    _record: _Identity
    _record_modes: tuple[int, ...] = (_RECORD_MODE,)


@dataclass(frozen=True, repr=False)
class PublicationStageHistoricalOwnership:
    """Reconciled ownership that authorizes cleanup but not publication."""

    _ownership: PublicationStageOwnership


@dataclass(frozen=True, slots=True, repr=False)
class PublicationStageOwnershipUncertainty:
    """No exact cleanup ownership was established."""


@dataclass(frozen=True, repr=False)
class PublicationStageCleanupDebt:
    """Exact remaining cleanup work after a bounded refusal."""

    _ownership: PublicationStageOwnership
    _stage_removed: bool

    @property
    def name(self) -> str:
        """Return the exact sibling basename without exposing it in repr."""
        return self._ownership._stage_name

    @property
    def device(self) -> int:
        """Return the exact sibling device identity."""
        return self._ownership._stage.device

    @property
    def inode(self) -> int:
        """Return the exact sibling inode identity."""
        return self._ownership._stage.inode


@dataclass(repr=False)
class PublicationRecordAcquisition:
    """Caller-owned record identity retained across partial creation."""

    identity: _Identity | None = None


@dataclass(repr=False)
class _OpenedScratchDirectory:
    directory_fd: int

    def close(self) -> BaseException | None:
        try:
            os.close(self.directory_fd)
        except OSError:
            pass
        except BaseException as control:
            return control
        return None


class PublicationReceiptError(Exception):
    """Closed bookkeeping failure with optional exact cleanup debt."""

    def __init__(
        self,
        kind: PublicationReceiptFailureKind,
        *,
        cleanup_debt: PublicationStageCleanupDebt | None = None,
    ) -> None:
        self.kind = kind
        self.cleanup_debt = cleanup_debt
        super().__init__(kind.value, cleanup_debt is not None)


PublicationStageReconciliation = PublicationStageHistoricalOwnership | PublicationStageOwnershipUncertainty


def publication_stage_name(token: bytes) -> str:
    """Derive the sole destination stage name from a core-generated token."""
    scratch_name(token)
    return _STAGE_PREFIX + token.hex()


def admit_publication_stage(
    scratch_parent_fd: int,
    ready: ReadyScratchReference,
    publication_parent_fd: int,
    *,
    expires_at: float | None = None,
) -> PublicationStageAdmission:
    """Validate and hold existing STAGE scratch before any destination mutation."""
    ownership = ready._reference._ownership
    if ownership._context.operation is not ScratchOperation.STAGE or not matches_receipt_context(ownership._context):
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
    _check_deadline(expires_at)
    opened = _open_scratch(scratch_parent_fd, ready, expires_at)
    try:
        parent = _fstat(publication_parent_fd)
        if not stat.S_ISDIR(parent.st_mode):
            raise PublicationReceiptError(PublicationReceiptFailureKind.UNSUPPORTED)
        name = publication_stage_name(ownership._token)
        if _stat_at(publication_parent_fd, name) is not None:
            raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
        if _stat_at(opened.directory_fd, _RECORD_NAME) is not None:
            raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
        _validate_scratch_receipt(opened.directory_fd, ownership, expires_at)
        _check_deadline(expires_at)
        return PublicationStageAdmission(opened, ownership, _identity(parent), name)
    except BaseException as error:
        close_control = opened.close()
        if close_control is not None:
            raise close_control from error
        raise


def record_publication_stage(
    admission: PublicationStageAdmission,
    publication_parent_fd: int,
    stage_fd: int,
    acquisition: PublicationRecordAcquisition,
    *,
    expires_at: float | None = None,
) -> PublicationStageOwnership:
    """Create and validate the immutable stage record exactly once."""
    if admission._closed:
        raise ValueError("Publication stage admission is not active")
    _check_deadline(expires_at)
    _validate_scratch_receipt(admission._opened.directory_fd, admission._scratch_ownership, expires_at)
    parent = _fstat(publication_parent_fd)
    if _identity(parent) != admission._publication_parent:
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
    descriptor_stage = _fstat(stage_fd)
    named_stage = _stat_at(publication_parent_fd, admission._stage_name)
    if named_stage is None:
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
    stage = _identity(descriptor_stage)
    if not _matches_stage(descriptor_stage, admission._publication_parent.device, stage):
        raise PublicationReceiptError(PublicationReceiptFailureKind.UNSUPPORTED)
    if not _matches_stage(named_stage, admission._publication_parent.device, stage):
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)

    descriptor: int | None = None
    result: PublicationStageOwnership | None = None
    prior: PublicationReceiptError | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(_RECORD_NAME, flags, _RECORD_BUILD_MODE, dir_fd=admission._opened.directory_fd)
        except OSError as error:
            kind = (
                PublicationReceiptFailureKind.CONFLICT
                if error.errno == errno.EEXIST
                else PublicationReceiptFailureKind.IO
            )
            raise PublicationReceiptError(kind) from None
        observed = _fstat(descriptor)
        acquisition.identity = _identity(observed)
        assert acquisition.identity is not None
        ownership = PublicationStageOwnership(
            admission._scratch_ownership,
            admission._publication_parent,
            admission._stage_name,
            stage,
            acquisition.identity,
        )
        content = _encode_record(ownership)
        _require_record_stat(observed, ownership, (_RECORD_BUILD_MODE,))
        _write_all(descriptor, content, expires_at)
        try:
            os.fchmod(descriptor, _RECORD_MODE)
        except OSError:
            raise PublicationReceiptError(PublicationReceiptFailureKind.IO) from None
        _check_deadline(expires_at)
        _require_unchanged_record(admission._opened.directory_fd, descriptor, ownership, len(content))
        if not hmac.compare_digest(_pread_bounded(descriptor, expires_at), content):
            raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
        _check_deadline(expires_at)
        result = ownership
    except PublicationReceiptError as error:
        if acquisition.identity is None:
            raise
        prior = PublicationReceiptError(
            error.kind,
            cleanup_debt=_acquired_record_cleanup_debt(admission, stage, acquisition.identity),
        )
        raise prior from None
    except BaseException as control:
        if acquisition.identity is None:
            raise
        prior = PublicationReceiptError(
            PublicationReceiptFailureKind.IO,
            cleanup_debt=_acquired_record_cleanup_debt(admission, stage, acquisition.identity),
        )
        raise control from prior
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
            except BaseException as control:
                if acquisition.identity is None:
                    raise
                if prior is None:
                    prior = PublicationReceiptError(
                        PublicationReceiptFailureKind.IO,
                        cleanup_debt=_acquired_record_cleanup_debt(admission, stage, acquisition.identity),
                    )
                raise control from prior
    assert result is not None
    try:
        _check_deadline(expires_at)
    except PublicationReceiptError as error:
        raise PublicationReceiptError(
            error.kind,
            cleanup_debt=PublicationStageCleanupDebt(result, False),
        ) from None
    return result


def _acquired_record_cleanup_debt(
    admission: PublicationStageAdmission,
    stage: _Identity,
    record: _Identity,
) -> PublicationStageCleanupDebt:
    ownership = PublicationStageOwnership(
        admission._scratch_ownership,
        admission._publication_parent,
        admission._stage_name,
        stage,
        record,
        (_RECORD_BUILD_MODE, _RECORD_MODE),
    )
    return PublicationStageCleanupDebt(ownership, False)


def reconcile_publication_stage(
    scratch_parent_fd: int,
    reference: ScratchReference,
    publication_parent_fd: int,
    *,
    expires_at: float | None = None,
) -> PublicationStageReconciliation:
    """Recover exact cleanup ownership without establishing publication proof."""
    ownership = reference._ownership
    if ownership._context.operation is not ScratchOperation.STAGE or not matches_receipt_context(ownership._context):
        return PublicationStageOwnershipUncertainty()
    opened: _OpenedScratchDirectory | None = None
    result: PublicationStageReconciliation = PublicationStageOwnershipUncertainty()
    prior: PublicationReceiptError | None = None
    try:
        _check_deadline(expires_at)
        opened = _open_scratch_reference(scratch_parent_fd, reference, expires_at)
        result = _reconcile_opened_publication_stage(
            opened.directory_fd,
            ownership,
            publication_parent_fd,
            expires_at,
        )
    except PublicationReceiptError as error:
        if error.kind is PublicationReceiptFailureKind.DEADLINE:
            prior = error
            raise
        result = PublicationStageOwnershipUncertainty()
    except (OSError, ValueError, ScratchTransferError):
        result = PublicationStageOwnershipUncertainty()
    finally:
        if opened is not None:
            close_control = opened.close()
            if close_control is not None:
                if prior is None:
                    prior = PublicationReceiptError(
                        PublicationReceiptFailureKind.IO,
                        cleanup_debt=_reconciliation_cleanup_debt(result),
                    )
                raise close_control from prior
    try:
        _check_deadline(expires_at)
    except PublicationReceiptError as error:
        raise PublicationReceiptError(
            error.kind,
            cleanup_debt=_reconciliation_cleanup_debt(result),
        ) from None
    return result


def _reconcile_opened_publication_stage(
    scratch_directory_fd: int,
    scratch_ownership: ScratchOwnership,
    publication_parent_fd: int,
    expires_at: float | None,
) -> PublicationStageReconciliation:
    parent = _fstat(publication_parent_fd)
    if not stat.S_ISDIR(parent.st_mode):
        return PublicationStageOwnershipUncertainty()
    record_fd: int | None = None
    prior: PublicationReceiptError | None = None
    try:
        record_fd = _open_record(scratch_directory_fd)
        record_stat = _fstat(record_fd)
        content = _pread_bounded(record_fd, expires_at)
        candidate = _decode_record(
            content,
            scratch_ownership,
            _identity(parent),
            _identity(record_stat),
        )
        expected = _encode_record(candidate)
        if not hmac.compare_digest(content, expected):
            return PublicationStageOwnershipUncertainty()
        _require_unchanged_record(scratch_directory_fd, record_fd, candidate, len(expected))
    except PublicationReceiptError as error:
        prior = error
        raise
    finally:
        if record_fd is not None:
            try:
                os.close(record_fd)
            except OSError:
                pass
            except BaseException as control:
                if prior is None:
                    raise
                raise control from prior
    stage = _stat_at(publication_parent_fd, candidate._stage_name)
    if stage is None or not _matches_stage(stage, candidate._publication_parent.device, candidate._stage):
        return PublicationStageOwnershipUncertainty()
    return PublicationStageHistoricalOwnership(candidate)


def _reconciliation_cleanup_debt(
    result: PublicationStageReconciliation,
) -> PublicationStageCleanupDebt | None:
    if isinstance(result, PublicationStageHistoricalOwnership):
        return PublicationStageCleanupDebt(result._ownership, False)
    return None


def cleanup_publication_stage(
    scratch_parent_fd: int,
    publication_parent_fd: int,
    owned: PublicationStageOwnership | PublicationStageHistoricalOwnership | PublicationStageCleanupDebt,
) -> None:
    """Remove the exact sibling before its exact immutable ownership record."""
    debt = _cleanup_debt(owned)
    ownership = debt._ownership
    opened: _OpenedScratchDirectory | None = None
    cleanup_complete = False
    prior: PublicationReceiptError | None = None
    try:
        opened = _open_owned_scratch(scratch_parent_fd, ownership._scratch_ownership)
        parent = _fstat(publication_parent_fd)
        if _identity(parent) != ownership._publication_parent:
            raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT, cleanup_debt=debt)
        record = _stat_at(opened.directory_fd, _RECORD_NAME)
        if record is None:
            if debt._stage_removed:
                cleanup_complete = True
                return
            raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT, cleanup_debt=debt)
        _require_record_stat(record, ownership, ownership._record_modes)
        if not debt._stage_removed:
            stage = _stat_at(publication_parent_fd, ownership._stage_name)
            if stage is None or not _matches_stage(stage, ownership._publication_parent.device, ownership._stage):
                raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT, cleanup_debt=debt)
            try:
                os.unlink(ownership._stage_name, dir_fd=publication_parent_fd)
            except OSError:
                raise PublicationReceiptError(PublicationReceiptFailureKind.IO, cleanup_debt=debt) from None
            debt = PublicationStageCleanupDebt(ownership, True)
        current_record = _stat_at(opened.directory_fd, _RECORD_NAME)
        if current_record is None or _identity(current_record) != ownership._record:
            raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT, cleanup_debt=debt)
        try:
            os.unlink(_RECORD_NAME, dir_fd=opened.directory_fd)
        except OSError:
            raise PublicationReceiptError(PublicationReceiptFailureKind.IO, cleanup_debt=debt) from None
        cleanup_complete = True
    except PublicationReceiptError as error:
        if error.cleanup_debt is not None:
            prior = error
            raise
        prior = PublicationReceiptError(error.kind, cleanup_debt=debt)
        raise prior from None
    except BaseException as control:
        cause = control.__cause__
        kind = cause.kind if isinstance(cause, PublicationReceiptError) else PublicationReceiptFailureKind.IO
        prior = PublicationReceiptError(kind, cleanup_debt=debt)
        raise control from prior
    finally:
        if opened is not None:
            close_control = opened.close()
            if close_control is not None:
                if prior is None:
                    prior = PublicationReceiptError(
                        PublicationReceiptFailureKind.IO,
                        cleanup_debt=None if cleanup_complete else debt,
                    )
                raise close_control from prior


def remove_publication_record(
    scratch_parent_fd: int,
    publication_parent_fd: int,
    ownership: PublicationStageOwnership | PublicationStageCleanupDebt,
) -> None:
    """Remove a record after the caller has independently proved publication."""
    debt = _cleanup_debt(ownership)
    record_only = PublicationStageCleanupDebt(debt._ownership, True)
    cleanup_publication_stage(scratch_parent_fd, publication_parent_fd, record_only)


def _open_scratch(
    parent_fd: int,
    ready: ReadyScratchReference,
    expires_at: float | None,
) -> _scratch._OpenedScratch:
    try:
        return _scratch._open_scratch(
            parent_fd,
            ready._reference,
            writable=False,
            phase=ScratchPhase.READ,
            expires_at=expires_at,
        )
    except ScratchTransferError as error:
        raise PublicationReceiptError(_map_scratch_failure(error.kind)) from None


def _open_scratch_reference(
    parent_fd: int,
    reference: ScratchReference,
    expires_at: float | None,
) -> _OpenedScratchDirectory:
    ownership = reference._ownership
    _check_deadline(expires_at)
    parent = _fstat(parent_fd)
    if (parent.st_dev, parent.st_ino) != (ownership._parent.device, ownership._parent.inode):
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
    name = scratch_name(ownership._token)
    named = _stat_at(parent_fd, name)
    if named is None or not matches_scratch_directory(
        named,
        ownership._directory,
        ownership._context.identity.euid,
        ownership._gid,
    ):
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        directory_fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        kind = (
            PublicationReceiptFailureKind.CONFLICT
            if error.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}
            else PublicationReceiptFailureKind.IO
        )
        raise PublicationReceiptError(kind) from None
    opened = _OpenedScratchDirectory(directory_fd)
    try:
        observed = _fstat(directory_fd)
        if not matches_scratch_directory(
            observed,
            ownership._directory,
            ownership._context.identity.euid,
            ownership._gid,
        ):
            raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
        _validate_scratch_receipt(directory_fd, ownership, expires_at)
        named = _stat_at(parent_fd, name)
        if named is None or (named.st_dev, named.st_ino) != (ownership._directory.device, ownership._directory.inode):
            raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
        _check_deadline(expires_at)
        return opened
    except BaseException as error:
        close_control = opened.close()
        if close_control is not None:
            raise close_control from error
        raise


def _open_owned_scratch(
    parent_fd: int,
    ownership: ScratchOwnership,
) -> _OpenedScratchDirectory:
    reference = _scratch.ScratchReference(ownership)
    return _open_scratch_reference(parent_fd, reference, None)


def _validate_scratch_receipt(
    directory_fd: int,
    ownership: ScratchOwnership,
    expires_at: float | None,
) -> None:
    try:
        validate_receipt(directory_fd, ownership, expires_at=expires_at)
    except ScratchReceiptError as error:
        raise PublicationReceiptError(_map_receipt_failure(error.kind)) from None


def _cleanup_debt(
    owned: PublicationStageOwnership | PublicationStageHistoricalOwnership | PublicationStageCleanupDebt,
) -> PublicationStageCleanupDebt:
    if isinstance(owned, PublicationStageCleanupDebt):
        return owned
    if isinstance(owned, PublicationStageHistoricalOwnership):
        return PublicationStageCleanupDebt(owned._ownership, False)
    if isinstance(owned, PublicationStageOwnership):
        return PublicationStageCleanupDebt(owned, False)
    raise ValueError("Publication stage ownership has an invalid type")


def _encode_record(ownership: PublicationStageOwnership) -> bytes:
    document = {
        "parent": _identity_document(ownership._publication_parent),
        "stage": {
            "device": ownership._stage.device,
            "inode": ownership._stage.inode,
            "name": ownership._stage_name,
        },
        "token": ownership._scratch_ownership._token.hex(),
        "version": 1,
    }
    encoded = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(encoded) > _MAX_RECORD_BYTES:
        raise ValueError("Publication record exceeds its bound")
    return encoded


def _decode_record(
    content: bytes,
    scratch_ownership: ScratchOwnership,
    publication_parent: _Identity,
    record: _Identity,
) -> PublicationStageOwnership:
    if not content or len(content) > _MAX_RECORD_BYTES or not content.isascii():
        raise ValueError("Invalid publication record")

    value = json.loads(content)
    if type(value) is not dict or set(value) != {"parent", "stage", "token", "version"}:
        raise ValueError("Invalid publication record")
    if value["version"] != 1 or value["token"] != scratch_ownership._token.hex():
        raise ValueError("Invalid publication record")
    parent = _decode_identity(value["parent"])
    if parent != publication_parent:
        raise ValueError("Invalid publication record")
    stage_value = value["stage"]
    if type(stage_value) is not dict or set(stage_value) != {"device", "inode", "name"}:
        raise ValueError("Invalid publication record")
    expected_name = publication_stage_name(scratch_ownership._token)
    if stage_value["name"] != expected_name:
        raise ValueError("Invalid publication record")
    stage = _decode_identity({"device": stage_value["device"], "inode": stage_value["inode"]})
    return PublicationStageOwnership(scratch_ownership, parent, expected_name, stage, record)


def _identity_document(identity: _Identity) -> dict[str, int]:
    return {"device": identity.device, "inode": identity.inode}


def _decode_identity(value: object) -> _Identity:
    if type(value) is not dict or set(value) != {"device", "inode"}:
        raise ValueError("Invalid publication record")
    device = value["device"]
    inode = value["inode"]
    if (
        type(device) is not int
        or type(inode) is not int
        or not 0 <= device <= _MAX_IDENTITY_NUMBER
        or not 0 <= inode <= _MAX_IDENTITY_NUMBER
    ):
        raise ValueError("Invalid publication record")
    return _Identity(device, inode)


def _require_unchanged_record(
    directory_fd: int,
    descriptor: int,
    ownership: PublicationStageOwnership,
    length: int,
) -> None:
    observed = _fstat(descriptor)
    _require_record_stat(observed, ownership, (_RECORD_MODE,))
    if observed.st_size != length:
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
    named = _stat_at(directory_fd, _RECORD_NAME)
    if named is None or _identity(named) != ownership._record:
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)


def _require_record_stat(
    observed: os.stat_result,
    ownership: PublicationStageOwnership,
    modes: tuple[int, ...],
) -> None:
    scratch = ownership._scratch_ownership
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or observed.st_uid != scratch._context.identity.euid
        or stat.S_IMODE(observed.st_mode) not in modes
        or _identity(observed) != ownership._record
    ):
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)


def _matches_stage(observed: os.stat_result, parent_device: int, identity: _Identity) -> bool:
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_nlink == 1
        and observed.st_dev == parent_device
        and _identity(observed) == identity
    )


def _open_record(directory_fd: int) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
    try:
        return os.open(_RECORD_NAME, flags, dir_fd=directory_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO, errno.ENODEV}:
            raise PublicationReceiptError(PublicationReceiptFailureKind.UNSUPPORTED) from None
        if error.errno == errno.ENOENT:
            raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT) from None
        raise PublicationReceiptError(PublicationReceiptFailureKind.IO) from None


def _pread_bounded(descriptor: int, expires_at: float | None) -> bytes:
    _check_deadline(expires_at)
    try:
        content = os.pread(descriptor, _MAX_RECORD_BYTES + 1, 0)
    except OSError:
        raise PublicationReceiptError(PublicationReceiptFailureKind.IO) from None
    _check_deadline(expires_at)
    if len(content) > _MAX_RECORD_BYTES:
        raise PublicationReceiptError(PublicationReceiptFailureKind.CONFLICT)
    return content


def _write_all(descriptor: int, content: bytes, expires_at: float | None) -> None:
    offset = 0
    while offset < len(content):
        _check_deadline(expires_at)
        try:
            written = os.write(descriptor, content[offset:])
        except OSError:
            raise PublicationReceiptError(PublicationReceiptFailureKind.IO) from None
        if written <= 0 or written > len(content) - offset:
            raise PublicationReceiptError(PublicationReceiptFailureKind.IO)
        offset += written


def _fstat(descriptor: int) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except OSError:
        raise PublicationReceiptError(PublicationReceiptFailureKind.IO) from None


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise PublicationReceiptError(PublicationReceiptFailureKind.IO) from None


def _identity(observed: os.stat_result) -> _Identity:
    return _Identity(observed.st_dev, observed.st_ino)


def _check_deadline(expires_at: float | None) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise PublicationReceiptError(PublicationReceiptFailureKind.DEADLINE)


def _map_receipt_failure(kind: ScratchReceiptFailureKind) -> PublicationReceiptFailureKind:
    return {
        ScratchReceiptFailureKind.UNSUPPORTED: PublicationReceiptFailureKind.UNSUPPORTED,
        ScratchReceiptFailureKind.CONFLICT: PublicationReceiptFailureKind.CONFLICT,
        ScratchReceiptFailureKind.DEADLINE: PublicationReceiptFailureKind.DEADLINE,
        ScratchReceiptFailureKind.IO: PublicationReceiptFailureKind.IO,
    }[kind]


def _map_scratch_failure(kind: ScratchFailureKind) -> PublicationReceiptFailureKind:
    if kind is ScratchFailureKind.UNSUPPORTED:
        return PublicationReceiptFailureKind.UNSUPPORTED
    if kind is ScratchFailureKind.DEADLINE:
        return PublicationReceiptFailureKind.DEADLINE
    if kind is ScratchFailureKind.IO:
        return PublicationReceiptFailureKind.IO
    return PublicationReceiptFailureKind.CONFLICT
