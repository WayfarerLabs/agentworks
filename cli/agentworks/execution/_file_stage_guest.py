"""Destination entry point for one private stage operation."""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress

from ._file_paths import ConfinedOpenError, open_linux_confined, open_linux_root
from ._file_stage_protocol import (
    MAX_REQUEST_BYTES,
    FileStageBeginRequest,
    FileStageChunkRequest,
    FileStageCleanupRequest,
    FileStageFailureCode,
    FileStageFailureControl,
    FileStageReconcileRequest,
    FileStageRequest,
    FileStageRequestError,
    decode_file_stage_request,
    empty_file_stage_body,
    encode_file_stage_begin_result,
    encode_file_stage_chunk_result,
    encode_file_stage_cleanup_result,
    encode_file_stage_failure,
    encode_file_stage_reconcile_result,
    stage_context,
)
from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity
from ._scratch import (
    ScratchFailureKind,
    ScratchPhase,
    ScratchReference,
    ScratchTransferError,
    _cleanup_debt,
    begin_scratch,
    cleanup_scratch,
    reconcile_scratch_ownership,
    write_scratch_chunk,
)
from ._scratch_receipt import ScratchHistoricalOwnership, ScratchOwnershipUncertainty

_OperationResult = ScratchReference | ScratchHistoricalOwnership | ScratchOwnershipUncertainty | None


class _SafeFailure(Exception):
    def __init__(self, failure: FileStageFailureControl) -> None:
        self.failure = failure
        super().__init__(failure.code.value)


def _read_request() -> FileStageRequest:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_file_stage_request(bytes(data))


def _expires_at(remaining_seconds: float | None) -> float | None:
    if remaining_seconds is None:
        return None
    return min(sys.float_info.max, time.monotonic() + remaining_seconds)


def _expired(expires_at: float | None) -> bool:
    return expires_at is not None and time.monotonic() >= expires_at


def _scratch_failure(error: ScratchTransferError) -> FileStageFailureControl:
    return FileStageFailureControl(
        FileStageFailureCode.SCRATCH,
        error.kind,
        error.phase,
        error.cleanup_debt,
    )


def _open_parent(request: FileStageRequest) -> tuple[int, int | None]:
    try:
        root_fd = open_linux_root(request.root_path)
    except ConfinedOpenError:
        raise _SafeFailure(FileStageFailureControl(FileStageFailureCode.ROOT_REFUSED)) from None
    if root_fd is None:
        raise _SafeFailure(FileStageFailureControl(FileStageFailureCode.ROOT_REFUSED))
    parent_fd: int | None = None
    try:
        components = request.relative_path.split("/")
        if len(components) > 1:
            parent_path = "/".join(components[:-1])
            flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                parent_fd = open_linux_confined(root_fd, parent_path, flags)
            except ConfinedOpenError:
                raise _SafeFailure(FileStageFailureControl(FileStageFailureCode.PARENT_REFUSED)) from None
            if parent_fd is None:
                raise _SafeFailure(FileStageFailureControl(FileStageFailureCode.PARENT_REFUSED))
        return root_fd, parent_fd
    except BaseException:
        with suppress(OSError):
            os.close(root_fd)
        raise


def _operate(request: FileStageRequest, expires_at: float | None) -> _OperationResult:
    root_fd, parent_fd = _open_parent(request)
    selected_fd = root_fd if parent_fd is None else parent_fd
    try:
        context = stage_context(request.identity)
        if isinstance(request, FileStageBeginRequest):
            return begin_scratch(
                selected_fd,
                request.expected_length,
                request.token,
                context,
                expires_at=expires_at,
            )
        if isinstance(request, FileStageChunkRequest):
            write_scratch_chunk(
                selected_fd,
                request.reference,
                request.offset,
                request.data,
                request.chunk_digest,
                expires_at=expires_at,
            )
            return None
        if isinstance(request, FileStageReconcileRequest):
            return reconcile_scratch_ownership(
                selected_fd,
                request.token,
                context,
                expires_at=expires_at,
            )
        assert isinstance(request, FileStageCleanupRequest)
        if _expired(expires_at):
            raise _SafeFailure(
                FileStageFailureControl(
                    FileStageFailureCode.SCRATCH,
                    ScratchFailureKind.DEADLINE,
                    ScratchPhase.CLEANUP,
                    request.cleanup_debt,
                )
            )
        cleanup_scratch(selected_fd, request.cleanup_debt)
        return None
    except ScratchTransferError as error:
        raise _SafeFailure(_scratch_failure(error)) from None
    finally:
        try:
            if parent_fd is not None:
                with suppress(OSError):
                    os.close(parent_fd)
        finally:
            with suppress(OSError):
                os.close(root_fd)


def _finish_failure(
    writer: FileRecordWriter,
    failure: FileStageFailureControl,
) -> int:
    writer.write(FileRecordKind.FAILED, encode_file_stage_failure(failure))
    writer.write(FileRecordKind.FINISHED, empty_file_stage_body())
    return 0


def _deadline_failure(request: FileStageRequest, result: _OperationResult) -> FileStageFailureControl:
    debt = None
    if isinstance(request, FileStageBeginRequest):
        if result is None:
            return FileStageFailureControl(FileStageFailureCode.DEADLINE)
        assert isinstance(result, ScratchReference)
        phase = ScratchPhase.BEGIN
        debt = _cleanup_debt(result)
    elif isinstance(request, FileStageChunkRequest):
        phase = ScratchPhase.WRITE
        debt = _cleanup_debt(request.reference)
    elif isinstance(request, FileStageReconcileRequest):
        phase = ScratchPhase.RECONCILE
        if isinstance(result, ScratchHistoricalOwnership):
            debt = _cleanup_debt(result)
    else:
        assert isinstance(request, FileStageCleanupRequest)
        phase = ScratchPhase.CLEANUP
        debt = request.cleanup_debt
    return FileStageFailureControl(FileStageFailureCode.SCRATCH, ScratchFailureKind.DEADLINE, phase, debt)


def main(nonce: str) -> int:
    """Execute one request after nonce, runtime, and exact identity checks."""
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
    except FileStageRequestError as error:
        return _finish_failure(writer, FileStageFailureControl(error.failure))
    if request.nonce != nonce:
        return _finish_failure(writer, FileStageFailureControl(FileStageFailureCode.NONCE_MISMATCH))
    if sys.platform != "linux":
        return _finish_failure(writer, FileStageFailureControl(FileStageFailureCode.UNSUPPORTED_RUNTIME))
    if not matches_current_identity(request.identity):
        return _finish_failure(writer, FileStageFailureControl(FileStageFailureCode.IDENTITY_MISMATCH))
    expires_at = _expires_at(request.remaining_seconds)
    if _expired(expires_at):
        return _finish_failure(writer, _deadline_failure(request, None))
    failure: FileStageFailureControl | None = None
    result: _OperationResult = None
    try:
        result = _operate(request, expires_at)
    except _SafeFailure as error:
        failure = error.failure
    if _expired(expires_at):
        if failure is None:
            failure = _deadline_failure(request, result)
        elif failure.code in {FileStageFailureCode.ROOT_REFUSED, FileStageFailureCode.PARENT_REFUSED}:
            failure = _deadline_failure(request, None)
    if failure is not None:
        return _finish_failure(writer, failure)
    if isinstance(request, FileStageBeginRequest):
        assert isinstance(result, ScratchReference)
        body = encode_file_stage_begin_result(result)
    elif isinstance(request, FileStageChunkRequest):
        body = encode_file_stage_chunk_result()
    elif isinstance(request, FileStageReconcileRequest):
        assert isinstance(result, ScratchHistoricalOwnership | ScratchOwnershipUncertainty)
        body = encode_file_stage_reconcile_result(result)
    else:
        assert isinstance(request, FileStageCleanupRequest)
        body = encode_file_stage_cleanup_result()
    writer.write(FileRecordKind.RESULT, body)
    writer.write(FileRecordKind.FINISHED, empty_file_stage_body())
    return 0
