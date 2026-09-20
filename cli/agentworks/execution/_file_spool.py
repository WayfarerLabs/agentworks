"""Bounded regular-file snapshots into exact private scratch."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import NoReturn

from ._file_snapshot import (
    SnapshotFailureKind,
    SnapshotReadError,
    _check_deadline,
    _hold_regular_file,
    _validate_inputs,
)
from ._file_stat import FileRevision
from ._scratch import (
    _MAX_CHUNK_BYTES,
    ReadyScratchReference,
    ScratchCleanupDebt,
    ScratchFailureKind,
    ScratchReference,
    ScratchTransferError,
    _cleanup_debt,
    begin_scratch,
    cleanup_scratch,
    verify_scratch,
    write_scratch_chunk,
)


class SpoolSnapshotFailureKind(Enum):
    """Closed snapshot failures that reveal no paths, bytes, or scratch names."""

    UNSUPPORTED_OBJECT = "unsupported_object"
    LIMIT = "limit"
    CONFLICT = "conflict"
    INTEGRITY = "integrity"
    DEADLINE = "deadline"
    IO = "io"


class SpoolSnapshotError(Exception):
    """A closed primary failure with optional exact cleanup debt."""

    def __init__(
        self,
        kind: SpoolSnapshotFailureKind,
        *,
        cleanup_debt: ScratchCleanupDebt | None = None,
    ) -> None:
        self.kind = kind
        self.cleanup_debt = cleanup_debt
        super().__init__(kind.value, cleanup_debt is not None)


@dataclass(frozen=True, slots=True, repr=False)
class SpoolSnapshot:
    """One verified private copy and its content-bound source observation."""

    ready: ReadyScratchReference
    source: FileRevision


def spool_snapshot(
    trusted_root_fd: int,
    relative_path: str,
    scratch_parent_fd: int,
    max_bytes: int,
    *,
    expires_at: float | None = None,
) -> SpoolSnapshot | None:
    """Copy one held regular source into ready scratch using bounded chunks.

    The caller owns both borrowed descriptors and its cooperating-writer lock.
    ``None`` means initial source absence. A failure after scratch creation makes
    one exact cleanup attempt outside the expired operation budget.
    """
    components = _validate_inputs(relative_path, max_bytes, expires_at)
    owned: ScratchReference | ReadyScratchReference | None = None
    ready: ReadyScratchReference | None = None
    source_revision: FileRevision | None = None
    primary: SpoolSnapshotFailureKind | None = None
    debt: ScratchCleanupDebt | None = None
    control: BaseException | None = None
    try:
        with _hold_regular_file(
            trusted_root_fd,
            components,
            max_bytes=max_bytes,
            stat_only=False,
            expires_at=expires_at,
        ) as source:
            if source is None:
                return None
            reference = begin_scratch(scratch_parent_fd, source.stat.size, expires_at=expires_at)
            owned = reference
            digest = hashlib.sha256()
            offset = 0
            while offset < source.stat.size:
                chunk = source.read(
                    min(_MAX_CHUNK_BYTES, source.stat.size - offset),
                    expires_at=expires_at,
                )
                if not chunk:
                    raise SnapshotReadError(SnapshotFailureKind.CONFLICT)
                digest.update(chunk)
                write_scratch_chunk(
                    scratch_parent_fd,
                    reference,
                    offset,
                    chunk,
                    hashlib.sha256(chunk).digest(),
                    expires_at=expires_at,
                )
                offset += len(chunk)
            if source.read(1, expires_at=expires_at):
                raise SnapshotReadError(SnapshotFailureKind.CONFLICT)
            whole_digest = digest.digest()
            ready = verify_scratch(
                scratch_parent_fd,
                reference,
                whole_digest,
                expires_at=expires_at,
            )
            owned = ready
            source.verify(expires_at=expires_at)
            source_revision = FileRevision(source.stat, whole_digest)
        _check_deadline(expires_at)
    except SnapshotReadError as error:
        primary = _source_failure(error.kind)
    except ScratchTransferError as error:
        primary = _scratch_failure(error.kind)
        debt = error.cleanup_debt
    except BaseException as error:
        control = error
        scratch_cause = error.__cause__
        if isinstance(scratch_cause, ScratchTransferError):
            primary = _scratch_failure(scratch_cause.kind)
            debt = scratch_cause.cleanup_debt

    if primary is None and control is None:
        assert ready is not None and source_revision is not None
        return SpoolSnapshot(ready, source_revision)

    if owned is not None:
        debt = _cleanup_debt(owned)
        cleanup_control: BaseException | None = None
        try:
            cleanup_scratch(scratch_parent_fd, owned)
        except ScratchTransferError as error:
            debt = error.cleanup_debt or debt
        except BaseException as error:
            cleanup_control = error
        else:
            debt = None
        if control is None and cleanup_control is not None:
            control = cleanup_control

    if control is not None:
        _raise_control(control, primary=primary, cleanup_debt=debt)
    assert primary is not None
    raise SpoolSnapshotError(primary, cleanup_debt=debt) from None


def _source_failure(kind: SnapshotFailureKind) -> SpoolSnapshotFailureKind:
    return {
        SnapshotFailureKind.UNSUPPORTED_OBJECT: SpoolSnapshotFailureKind.UNSUPPORTED_OBJECT,
        SnapshotFailureKind.LIMIT: SpoolSnapshotFailureKind.LIMIT,
        SnapshotFailureKind.CONFLICT: SpoolSnapshotFailureKind.CONFLICT,
        SnapshotFailureKind.DEADLINE: SpoolSnapshotFailureKind.DEADLINE,
        SnapshotFailureKind.IO: SpoolSnapshotFailureKind.IO,
    }[kind]


def _scratch_failure(kind: ScratchFailureKind) -> SpoolSnapshotFailureKind:
    return {
        ScratchFailureKind.UNSUPPORTED: SpoolSnapshotFailureKind.UNSUPPORTED_OBJECT,
        ScratchFailureKind.CONFLICT: SpoolSnapshotFailureKind.CONFLICT,
        ScratchFailureKind.LIMIT: SpoolSnapshotFailureKind.LIMIT,
        ScratchFailureKind.INTEGRITY: SpoolSnapshotFailureKind.INTEGRITY,
        ScratchFailureKind.DEADLINE: SpoolSnapshotFailureKind.DEADLINE,
        ScratchFailureKind.IO: SpoolSnapshotFailureKind.IO,
    }[kind]


def _raise_control(
    control: BaseException,
    *,
    primary: SpoolSnapshotFailureKind | None,
    cleanup_debt: ScratchCleanupDebt | None,
) -> NoReturn:
    if primary is None and cleanup_debt is None:
        raise control
    raise control from SpoolSnapshotError(
        SpoolSnapshotFailureKind.IO if primary is None else primary,
        cleanup_debt=cleanup_debt,
    )
