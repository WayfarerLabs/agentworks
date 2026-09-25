"""Destination entry point for one bounded identity-bound Linux file read."""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress

from ._file_paths import ConfinedOpenError, open_linux_root
from ._file_read_protocol import (
    MAX_REQUEST_BYTES,
    FileReadFailure,
    FileReadRequest,
    FileReadRequestError,
    FileReadResultControl,
    decode_file_read_request,
    empty_file_read_body,
    encode_file_read_failure,
    encode_file_read_result,
)
from ._file_snapshot import FileSnapshot, SnapshotFailureKind, SnapshotReadError, read_snapshot
from ._file_wire import MAX_RECORD_BODY_BYTES, FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity


class _SafeFailure(Exception):
    def __init__(self, failure: FileReadFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


def _read_request() -> FileReadRequest:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_file_read_request(bytes(data))


def _failure_for_snapshot(error: SnapshotReadError) -> FileReadFailure:
    return {
        SnapshotFailureKind.UNSUPPORTED_OBJECT: FileReadFailure.UNSUPPORTED_OBJECT,
        SnapshotFailureKind.LIMIT: FileReadFailure.LIMIT,
        SnapshotFailureKind.CONFLICT: FileReadFailure.CONFLICT,
        SnapshotFailureKind.DEADLINE: FileReadFailure.DEADLINE,
        SnapshotFailureKind.IO: FileReadFailure.IO,
    }[error.kind]


def _expires_at(remaining_seconds: float | None) -> float | None:
    if remaining_seconds is None:
        return None
    return min(sys.float_info.max, time.monotonic() + remaining_seconds)


def _raise_if_expired(expires_at: float | None) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise _SafeFailure(FileReadFailure.DEADLINE)


def _snapshot(request: FileReadRequest, expires_at: float | None) -> FileSnapshot | None:
    try:
        root_fd = open_linux_root(request.root_path)
    except ConfinedOpenError:
        raise _SafeFailure(FileReadFailure.ROOT_REFUSED) from None
    if root_fd is None:
        return None
    try:
        return read_snapshot(root_fd, request.relative_path, request.max_bytes, expires_at=expires_at)
    except SnapshotReadError as error:
        raise _SafeFailure(_failure_for_snapshot(error)) from None
    finally:
        with suppress(OSError):
            os.close(root_fd)


def _emit_snapshot(writer: FileRecordWriter, snapshot: FileSnapshot) -> None:
    for offset in range(0, len(snapshot.data), MAX_RECORD_BODY_BYTES):
        writer.write(FileRecordKind.DATA, snapshot.data[offset : offset + MAX_RECORD_BODY_BYTES])
    observed = snapshot.stat
    writer.write(
        FileRecordKind.RESULT,
        encode_file_read_result(
            FileReadResultControl(
                digest=snapshot.digest,
                metadata=observed,
            )
        ),
    )


def _finish_failure(writer: FileRecordWriter, failure: FileReadFailure) -> int:
    writer.write(FileRecordKind.FAILED, encode_file_read_failure(failure))
    writer.write(FileRecordKind.FINISHED, empty_file_read_body())
    return 0


def main(nonce: str) -> int:
    """Read one request only after exact identity verification."""
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
    except FileReadRequestError as error:
        return _finish_failure(writer, error.failure)
    if request.nonce != nonce:
        return _finish_failure(writer, FileReadFailure.NONCE_MISMATCH)
    if sys.platform != "linux":
        return _finish_failure(writer, FileReadFailure.UNSUPPORTED_RUNTIME)
    if not matches_current_identity(request.identity):
        return _finish_failure(writer, FileReadFailure.IDENTITY_MISMATCH)
    try:
        expires_at = _expires_at(request.remaining_seconds)
        _raise_if_expired(expires_at)
        snapshot = _snapshot(request, expires_at)
        _raise_if_expired(expires_at)
    except _SafeFailure as error:
        return _finish_failure(writer, error.failure)
    if snapshot is None:
        writer.write(FileRecordKind.ABSENT, empty_file_read_body())
    else:
        _emit_snapshot(writer, snapshot)
    writer.write(FileRecordKind.FINISHED, empty_file_read_body())
    return 0
