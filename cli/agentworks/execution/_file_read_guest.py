"""Destination entry point for one bounded identity-bound Linux file read."""

from __future__ import annotations

import errno
import os
import sys
from contextlib import suppress

from ._file_read_protocol import (
    MAX_RECORD_BODY_BYTES,
    MAX_REQUEST_BYTES,
    FileReadFailure,
    FileReadRecord,
    FileReadRecordKind,
    FileReadRequest,
    FileReadRequestError,
    FileReadResultControl,
    decode_file_read_request,
    empty_file_read_body,
    encode_file_read_failure,
    encode_file_read_record,
    encode_file_read_result,
)
from ._file_snapshot import FileSnapshot, SnapshotFailureKind, SnapshotReadError, read_snapshot
from ._helper_identity import matches_current_identity


class _Emitter:
    def __init__(self, nonce: str) -> None:
        self._nonce = nonce
        self._sequence = 0

    def emit(self, kind: FileReadRecordKind, body: bytes) -> None:
        record = encode_file_read_record(self._nonce, FileReadRecord(self._sequence, kind, body))
        remaining = memoryview(record)
        while remaining:
            written = os.write(1, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        self._sequence += 1


def _open_trusted_root(path: str) -> int | None:
    """Open every absolute ancestor without links, permitting mounts to the root."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open("/", flags)
    except OSError:
        raise _SafeFailure(FileReadFailure.ROOT_REFUSED) from None
    components = () if path == "/" else path[1:].split("/")
    for component in components:
        error_number: int | None = None
        try:
            child = os.open(component, flags, dir_fd=descriptor)
        except OSError as error:
            error_number = error.errno
            child = None
        if child is None:
            with suppress(OSError):
                os.close(descriptor)
            if error_number == errno.ENOENT:
                return None
            raise _SafeFailure(FileReadFailure.ROOT_REFUSED)
        previous = descriptor
        descriptor = child
        with suppress(OSError):
            os.close(previous)
    return descriptor


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
        SnapshotFailureKind.IO: FileReadFailure.IO,
    }[error.kind]


def _snapshot(request: FileReadRequest) -> FileSnapshot | None:
    root_fd = _open_trusted_root(request.root_path)
    if root_fd is None:
        return None
    try:
        return read_snapshot(root_fd, request.relative_path, request.max_bytes)
    except SnapshotReadError as error:
        raise _SafeFailure(_failure_for_snapshot(error)) from None
    finally:
        with suppress(OSError):
            os.close(root_fd)


def _emit_snapshot(emitter: _Emitter, snapshot: FileSnapshot) -> None:
    for offset in range(0, len(snapshot.data), MAX_RECORD_BODY_BYTES):
        emitter.emit(FileReadRecordKind.DATA, snapshot.data[offset : offset + MAX_RECORD_BODY_BYTES])
    observed = snapshot.stat
    emitter.emit(
        FileReadRecordKind.RESULT,
        encode_file_read_result(
            FileReadResultControl(
                digest=snapshot.digest,
                metadata=observed,
            )
        ),
    )


def _finish_failure(emitter: _Emitter, failure: FileReadFailure) -> int:
    emitter.emit(FileReadRecordKind.FAILED, encode_file_read_failure(failure))
    emitter.emit(FileReadRecordKind.FINISHED, empty_file_read_body())
    return 0


def main(nonce: str) -> int:
    """Read one request only after exact identity verification."""
    emitter = _Emitter(nonce)
    try:
        request = _read_request()
    except FileReadRequestError as error:
        return _finish_failure(emitter, error.failure)
    if request.nonce != nonce:
        return _finish_failure(emitter, FileReadFailure.NONCE_MISMATCH)
    if sys.platform != "linux":
        return _finish_failure(emitter, FileReadFailure.UNSUPPORTED_RUNTIME)
    if not matches_current_identity(request.identity):
        return _finish_failure(emitter, FileReadFailure.IDENTITY_MISMATCH)
    try:
        snapshot = _snapshot(request)
    except _SafeFailure as error:
        return _finish_failure(emitter, error.failure)
    if snapshot is None:
        emitter.emit(FileReadRecordKind.ABSENT, empty_file_read_body())
    else:
        _emit_snapshot(emitter, snapshot)
    emitter.emit(FileReadRecordKind.FINISHED, empty_file_read_body())
    return 0
