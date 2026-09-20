"""Destination entry point for one Linux metadata operation."""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress

from ._file_metadata import MetadataError, MetadataResult, ensure_directory, set_metadata
from ._file_metadata_protocol import (
    MAX_REQUEST_BYTES,
    FileMetadataFailureCode,
    FileMetadataFailureControl,
    FileMetadataOperation,
    FileMetadataRequest,
    FileMetadataRequestError,
    FileMetadataResultControl,
    FileMetadataResultKind,
    decode_file_metadata_request,
    empty_file_metadata_body,
    encode_file_metadata_failure,
    encode_file_metadata_result,
)
from ._file_paths import ConfinedOpenError, open_linux_confined, open_linux_root
from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity


class _SafeFailure(Exception):
    def __init__(self, failure: FileMetadataFailureControl) -> None:
        self.failure = failure
        super().__init__(failure.code.value)


def _read_request() -> FileMetadataRequest:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_file_metadata_request(bytes(data))


def _parent(request: FileMetadataRequest) -> tuple[int, int | None, str]:
    try:
        root_fd = open_linux_root(request.root_path)
    except ConfinedOpenError:
        raise _SafeFailure(FileMetadataFailureControl(FileMetadataFailureCode.ROOT_REFUSED)) from None
    if root_fd is None:
        raise _SafeFailure(FileMetadataFailureControl(FileMetadataFailureCode.ROOT_REFUSED))
    components = request.relative_path.split("/")
    leaf = components[-1]
    if len(components) == 1:
        return root_fd, None, leaf
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        parent_fd = open_linux_confined(root_fd, "/".join(components[:-1]), flags)
        if parent_fd is None:
            raise _SafeFailure(FileMetadataFailureControl(FileMetadataFailureCode.PARENT_REFUSED))
    except ConfinedOpenError:
        failure: BaseException = _SafeFailure(FileMetadataFailureControl(FileMetadataFailureCode.PARENT_REFUSED))
    except BaseException as error:
        failure = error
    else:
        return root_fd, parent_fd, leaf
    with suppress(OSError):
        os.close(root_fd)
    raise failure


def _metadata_failure(error: MetadataError) -> FileMetadataFailureControl:
    return FileMetadataFailureControl(
        FileMetadataFailureCode.METADATA,
        error.kind,
        error.phase,
        error.completed_steps,
        error.attempted_step,
    )


def _operate(request: FileMetadataRequest, expires_at: float | None) -> FileMetadataResultControl:
    root_fd, parent_fd, leaf = _parent(request)
    descriptor = root_fd if parent_fd is None else parent_fd
    try:
        function = set_metadata if request.operation is FileMetadataOperation.SET_METADATA else ensure_directory
        result: MetadataResult = function(
            descriptor,
            leaf,
            uid=request.uid,
            gid=request.gid,
            mode=request.mode,
            expires_at=expires_at,
        )
        return FileMetadataResultControl(
            FileMetadataResultKind.CHANGED if result.changed else FileMetadataResultKind.UNCHANGED,
            result.revision,
        )
    except MetadataError as error:
        raise _SafeFailure(_metadata_failure(error)) from None
    finally:
        if parent_fd is not None:
            with suppress(OSError):
                os.close(parent_fd)
        with suppress(OSError):
            os.close(root_fd)


def _finish_failure(writer: FileRecordWriter, failure: FileMetadataFailureControl) -> int:
    writer.write(FileRecordKind.FAILED, encode_file_metadata_failure(failure))
    writer.write(FileRecordKind.FINISHED, empty_file_metadata_body())
    return 0


def main(nonce: str) -> int:
    """Execute one request after nonce, runtime and exact identity checks."""
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
    except FileMetadataRequestError as error:
        return _finish_failure(writer, FileMetadataFailureControl(error.failure))
    if request.nonce != nonce:
        return _finish_failure(writer, FileMetadataFailureControl(FileMetadataFailureCode.NONCE_MISMATCH))
    if sys.platform != "linux":
        return _finish_failure(writer, FileMetadataFailureControl(FileMetadataFailureCode.UNSUPPORTED_RUNTIME))
    if not matches_current_identity(request.identity):
        return _finish_failure(writer, FileMetadataFailureControl(FileMetadataFailureCode.IDENTITY_MISMATCH))
    expires_at = None if request.remaining_seconds is None else time.monotonic() + request.remaining_seconds
    failure: FileMetadataFailureControl | None = None
    result: FileMetadataResultControl | None = None
    try:
        result = _operate(request, expires_at)
    except _SafeFailure as error:
        failure = error.failure
    if failure is not None:
        return _finish_failure(writer, failure)
    assert result is not None
    writer.write(FileRecordKind.RESULT, encode_file_metadata_result(result))
    writer.write(FileRecordKind.FINISHED, empty_file_metadata_body())
    return 0
