"""Destination entry point for one private snapshot operation."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from contextlib import suppress

from ._file_paths import ConfinedOpenError, open_linux_root
from ._file_snapshot_protocol import (
    MAX_REQUEST_BYTES,
    FileSnapshotBeginRequest,
    FileSnapshotChunkRequest,
    FileSnapshotChunkResult,
    FileSnapshotCleanupRequest,
    FileSnapshotFailureCode,
    FileSnapshotFailureControl,
    FileSnapshotReconcileRequest,
    FileSnapshotRequest,
    FileSnapshotRequestError,
    decode_file_snapshot_request,
    empty_file_snapshot_body,
    encode_file_snapshot_begin_result,
    encode_file_snapshot_chunk_result,
    encode_file_snapshot_cleanup_result,
    encode_file_snapshot_failure,
    encode_file_snapshot_reconcile_result,
    snapshot_context,
)
from ._file_spool import SpoolSnapshot, SpoolSnapshotError, SpoolSnapshotFailureKind, spool_snapshot
from ._file_wire import MAX_RECORD_BODY_BYTES, FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity
from ._scratch import (
    ScratchFailureKind,
    ScratchPhase,
    ScratchTransferError,
    _cleanup_debt,
    cleanup_scratch,
    read_scratch_range,
    reconcile_scratch_ownership,
)
from ._scratch_receipt import ScratchHistoricalOwnership, ScratchOwnershipUncertainty
from ._scratch_root import ScratchRootError, ScratchRootFailureKind, open_scratch_root

_OperationResult = SpoolSnapshot | bytes | ScratchHistoricalOwnership | ScratchOwnershipUncertainty | None


class _SafeFailure(Exception):
    def __init__(self, failure: FileSnapshotFailureControl) -> None:
        self.failure = failure
        super().__init__(failure.code.value)


def _read_request() -> FileSnapshotRequest:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_file_snapshot_request(bytes(data))


def _expires_at(remaining_seconds: float | None) -> float | None:
    if remaining_seconds is None:
        return None
    return min(sys.float_info.max, time.monotonic() + remaining_seconds)


def _expired(expires_at: float | None) -> bool:
    return expires_at is not None and time.monotonic() >= expires_at


def _deadline_failure(
    request: FileSnapshotRequest,
    result: _OperationResult,
) -> FileSnapshotFailureControl:
    if isinstance(request, FileSnapshotBeginRequest):
        debt = None if not isinstance(result, SpoolSnapshot) else _cleanup_debt(result.ready)
        return FileSnapshotFailureControl(
            FileSnapshotFailureCode.SPOOL,
            spool_kind=SpoolSnapshotFailureKind.DEADLINE,
            cleanup_debt=debt,
        )
    phase = {
        FileSnapshotChunkRequest: ScratchPhase.READ,
        FileSnapshotReconcileRequest: ScratchPhase.RECONCILE,
        FileSnapshotCleanupRequest: ScratchPhase.CLEANUP,
    }[type(request)]
    debt = None
    if isinstance(request, FileSnapshotChunkRequest):
        debt = _cleanup_debt(request.ready)
    elif isinstance(request, FileSnapshotCleanupRequest):
        debt = request.cleanup_debt
    elif isinstance(result, ScratchHistoricalOwnership):
        debt = _cleanup_debt(result)
    return FileSnapshotFailureControl(
        FileSnapshotFailureCode.SCRATCH,
        scratch_kind=ScratchFailureKind.DEADLINE,
        scratch_phase=phase,
        cleanup_debt=debt,
    )


def _operate_begin(
    request: FileSnapshotBeginRequest,
    scratch_fd: int,
    expires_at: float | None,
) -> SpoolSnapshot | None:
    try:
        source_fd = open_linux_root(request.root_path)
    except ConfinedOpenError:
        raise _SafeFailure(FileSnapshotFailureControl(FileSnapshotFailureCode.ROOT_REFUSED)) from None
    if source_fd is None:
        raise _SafeFailure(FileSnapshotFailureControl(FileSnapshotFailureCode.ROOT_REFUSED))
    try:
        return spool_snapshot(
            source_fd,
            request.relative_path,
            scratch_fd,
            request.max_bytes,
            request.token,
            request.identity,
            expires_at=expires_at,
        )
    except SpoolSnapshotError as error:
        raise _SafeFailure(
            FileSnapshotFailureControl(
                FileSnapshotFailureCode.SPOOL,
                spool_kind=error.kind,
                cleanup_debt=error.cleanup_debt,
            )
        ) from None
    finally:
        with suppress(OSError):
            os.close(source_fd)


def _operate(request: FileSnapshotRequest, expires_at: float | None) -> _OperationResult:
    try:
        scratch_fd = open_scratch_root(expires_at=expires_at)
    except ScratchRootError as error:
        if error.kind is ScratchRootFailureKind.DEADLINE:
            raise _SafeFailure(_deadline_failure(request, None)) from None
        raise _SafeFailure(FileSnapshotFailureControl(FileSnapshotFailureCode.SCRATCH_ROOT_REFUSED)) from None
    try:
        if isinstance(request, FileSnapshotBeginRequest):
            return _operate_begin(request, scratch_fd, expires_at)
        if isinstance(request, FileSnapshotChunkRequest):
            return read_scratch_range(
                scratch_fd,
                request.ready,
                request.offset,
                request.length,
                expires_at=expires_at,
            )
        if isinstance(request, FileSnapshotReconcileRequest):
            return reconcile_scratch_ownership(
                scratch_fd,
                request.token,
                snapshot_context(request.identity),
                expires_at=expires_at,
            )
        assert isinstance(request, FileSnapshotCleanupRequest)
        if _expired(expires_at):
            raise _SafeFailure(_deadline_failure(request, None))
        cleanup_scratch(scratch_fd, request.cleanup_debt)
        return None
    except ScratchTransferError as error:
        raise _SafeFailure(
            FileSnapshotFailureControl(
                FileSnapshotFailureCode.SCRATCH,
                scratch_kind=error.kind,
                scratch_phase=error.phase,
                cleanup_debt=error.cleanup_debt,
            )
        ) from None
    finally:
        with suppress(OSError):
            os.close(scratch_fd)


def _finish_failure(writer: FileRecordWriter, failure: FileSnapshotFailureControl) -> int:
    writer.write(FileRecordKind.FAILED, encode_file_snapshot_failure(failure))
    writer.write(FileRecordKind.FINISHED, empty_file_snapshot_body())
    return 0


def _finish_result(
    writer: FileRecordWriter,
    request: FileSnapshotRequest,
    result: _OperationResult,
) -> int:
    if isinstance(request, FileSnapshotBeginRequest):
        assert result is None or isinstance(result, SpoolSnapshot)
        body = encode_file_snapshot_begin_result(result)
    elif isinstance(request, FileSnapshotChunkRequest):
        assert isinstance(result, bytes)
        for offset in range(0, len(result), MAX_RECORD_BODY_BYTES):
            writer.write(FileRecordKind.DATA, result[offset : offset + MAX_RECORD_BODY_BYTES])
        body = encode_file_snapshot_chunk_result(
            FileSnapshotChunkResult(
                request.offset,
                len(result),
                result,
                hashlib.sha256(result).digest(),
            )
        )
    elif isinstance(request, FileSnapshotReconcileRequest):
        assert isinstance(result, ScratchHistoricalOwnership | ScratchOwnershipUncertainty)
        body = encode_file_snapshot_reconcile_result(result)
    else:
        assert isinstance(request, FileSnapshotCleanupRequest) and result is None
        body = encode_file_snapshot_cleanup_result()
    writer.write(FileRecordKind.RESULT, body)
    writer.write(FileRecordKind.FINISHED, empty_file_snapshot_body())
    return 0


def main(nonce: str) -> int:
    """Execute one request after nonce, runtime, and exact identity checks."""
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
    except FileSnapshotRequestError as error:
        return _finish_failure(writer, FileSnapshotFailureControl(error.failure))
    if request.nonce != nonce:
        return _finish_failure(writer, FileSnapshotFailureControl(FileSnapshotFailureCode.NONCE_MISMATCH))
    if sys.platform != "linux":
        return _finish_failure(writer, FileSnapshotFailureControl(FileSnapshotFailureCode.UNSUPPORTED_RUNTIME))
    if not matches_current_identity(request.identity):
        return _finish_failure(writer, FileSnapshotFailureControl(FileSnapshotFailureCode.IDENTITY_MISMATCH))

    expires_at = _expires_at(request.remaining_seconds)
    if _expired(expires_at):
        return _finish_failure(writer, _deadline_failure(request, None))
    failure: FileSnapshotFailureControl | None = None
    result: _OperationResult = None
    try:
        result = _operate(request, expires_at)
    except _SafeFailure as error:
        failure = error.failure
    if _expired(expires_at) and (
        failure is None
        or failure.code
        in {
            FileSnapshotFailureCode.ROOT_REFUSED,
            FileSnapshotFailureCode.SCRATCH_ROOT_REFUSED,
        }
    ):
        failure = _deadline_failure(request, result)
    if failure is not None:
        return _finish_failure(writer, failure)
    return _finish_result(writer, request, result)
