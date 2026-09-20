"""Destination entry point for one locked private stage operation."""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress

from ._file_lock import FileLockError, FileLockFailureKind, system_file_lock
from ._file_paths import ConfinedOpenError, open_linux_confined, open_linux_root
from ._file_stage_protocol import (
    MAX_REQUEST_BYTES,
    FileStageBeginRequest,
    FileStageChunkRequest,
    FileStageFailureCode,
    FileStageFailureControl,
    FileStageRequest,
    FileStageRequestError,
    decode_file_stage_request,
    empty_file_stage_body,
    encode_file_stage_begin_result,
    encode_file_stage_chunk_result,
    encode_file_stage_failure,
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
    write_scratch_chunk,
)


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


def _lock_failure(error: FileLockError) -> FileStageFailureControl:
    code = {
        FileLockFailureKind.UNSUPPORTED: FileStageFailureCode.LOCK_UNSUPPORTED,
        FileLockFailureKind.MISSING: FileStageFailureCode.LOCK_MISSING,
        FileLockFailureKind.UNSAFE: FileStageFailureCode.LOCK_UNSAFE,
        FileLockFailureKind.CONFLICT: FileStageFailureCode.LOCK_CONFLICT,
        FileLockFailureKind.DEADLINE: FileStageFailureCode.LOCK_DEADLINE,
        FileLockFailureKind.IO: FileStageFailureCode.LOCK_IO,
    }[error.kind]
    return FileStageFailureControl(code)


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


def _operate(request: FileStageRequest, expires_at: float | None) -> ScratchReference | None:
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
        assert isinstance(request, FileStageChunkRequest)
        write_scratch_chunk(
            selected_fd,
            request.reference,
            request.offset,
            request.data,
            request.chunk_digest,
            expires_at=expires_at,
        )
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
    failure: FileStageFailureControl | None = None
    result: ScratchReference | None = None
    try:
        with system_file_lock(expires_at=expires_at):
            try:
                result = _operate(request, expires_at)
            except _SafeFailure as error:
                failure = error.failure
    except FileLockError as error:
        return _finish_failure(writer, _lock_failure(error))
    if _expired(expires_at):
        if failure is None:
            if isinstance(request, FileStageBeginRequest):
                assert result is not None
                phase = ScratchPhase.BEGIN
                debt = _cleanup_debt(result)
            else:
                phase = ScratchPhase.WRITE
                debt = _cleanup_debt(request.reference)
            failure = FileStageFailureControl(FileStageFailureCode.SCRATCH, ScratchFailureKind.DEADLINE, phase, debt)
        elif failure.code in {FileStageFailureCode.ROOT_REFUSED, FileStageFailureCode.PARENT_REFUSED}:
            failure = FileStageFailureControl(FileStageFailureCode.LOCK_DEADLINE)
    if failure is not None:
        return _finish_failure(writer, failure)
    if isinstance(request, FileStageBeginRequest):
        assert result is not None
        body = encode_file_stage_begin_result(result)
    else:
        body = encode_file_stage_chunk_result()
    writer.write(FileRecordKind.RESULT, body)
    writer.write(FileRecordKind.FINISHED, empty_file_stage_body())
    return 0
