"""Destination entry point for one locked bounded Linux directory inventory."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from contextlib import suppress

from ._file_inventory import FileInventoryError, FileInventoryFailureKind, encode_inventory, inventory_directory
from ._file_inventory_protocol import (
    MAX_REQUEST_BYTES,
    FileInventoryFailureCode,
    FileInventoryRequest,
    FileInventoryRequestError,
    FileInventoryResultControl,
    decode_file_inventory_request,
    empty_file_inventory_body,
    encode_file_inventory_failure,
    encode_file_inventory_result,
)
from ._file_lock import FileLockError, FileLockFailureKind, system_file_lock
from ._file_paths import ConfinedOpenError, open_linux_confined, open_linux_root
from ._file_wire import MAX_RECORD_BODY_BYTES, FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class _SafeFailure(Exception):
    def __init__(self, failure: FileInventoryFailureCode) -> None:
        self.failure = failure
        super().__init__(failure.value)


def _read_request() -> FileInventoryRequest:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_file_inventory_request(bytes(data))


def _lock_failure(error: FileLockError) -> FileInventoryFailureCode:
    return {
        FileLockFailureKind.UNSUPPORTED: FileInventoryFailureCode.LOCK_UNSUPPORTED,
        FileLockFailureKind.MISSING: FileInventoryFailureCode.LOCK_MISSING,
        FileLockFailureKind.UNSAFE: FileInventoryFailureCode.LOCK_UNSAFE,
        FileLockFailureKind.CONFLICT: FileInventoryFailureCode.LOCK_CONFLICT,
        FileLockFailureKind.DEADLINE: FileInventoryFailureCode.LOCK_DEADLINE,
        FileLockFailureKind.IO: FileInventoryFailureCode.LOCK_IO,
    }[error.kind]


def _inventory_failure(error: FileInventoryError) -> FileInventoryFailureCode:
    return {
        FileInventoryFailureKind.UNSUPPORTED: FileInventoryFailureCode.UNSUPPORTED_OBJECT,
        FileInventoryFailureKind.LIMIT: FileInventoryFailureCode.LIMIT,
        FileInventoryFailureKind.CONFLICT: FileInventoryFailureCode.CONFLICT,
        FileInventoryFailureKind.DEADLINE: FileInventoryFailureCode.DEADLINE,
        FileInventoryFailureKind.IO: FileInventoryFailureCode.IO,
    }[error.kind]


def _expires_at(remaining_seconds: float | None) -> float | None:
    if remaining_seconds is None:
        return None
    return min(sys.float_info.max, time.monotonic() + remaining_seconds)


def _raise_if_expired(expires_at: float | None) -> None:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise _SafeFailure(FileInventoryFailureCode.DEADLINE)


def _snapshot(request: FileInventoryRequest, expires_at: float | None) -> bytes | None:
    try:
        root_fd = open_linux_root(request.root_path)
    except ConfinedOpenError:
        raise _SafeFailure(FileInventoryFailureCode.ROOT_REFUSED) from None
    if root_fd is None:
        return None
    target_fd: int | None = None
    try:
        try:
            target_fd = open_linux_confined(root_fd, request.relative_path, _DIRECTORY_FLAGS)
        except ConfinedOpenError:
            raise _SafeFailure(FileInventoryFailureCode.TARGET_REFUSED) from None
        if target_fd is None:
            return None
        try:
            entries = inventory_directory(
                target_fd,
                max_entries=request.max_entries,
                max_depth=request.max_depth,
                max_encoded_bytes=request.max_encoded_bytes,
                expires_at=expires_at,
            )
            encoded = encode_inventory(entries)
            return encoded
        except FileInventoryError as error:
            raise _SafeFailure(_inventory_failure(error)) from None
    finally:
        if target_fd is not None:
            with suppress(OSError):
                os.close(target_fd)
        with suppress(OSError):
            os.close(root_fd)


def _emit_snapshot(writer: FileRecordWriter, snapshot: bytes) -> None:
    for offset in range(0, len(snapshot), MAX_RECORD_BODY_BYTES):
        writer.write(FileRecordKind.DATA, snapshot[offset : offset + MAX_RECORD_BODY_BYTES])
    writer.write(
        FileRecordKind.RESULT,
        encode_file_inventory_result(FileInventoryResultControl(len(snapshot), hashlib.sha256(snapshot).digest())),
    )


def _finish_failure(writer: FileRecordWriter, failure: FileInventoryFailureCode) -> int:
    writer.write(FileRecordKind.FAILED, encode_file_inventory_failure(failure))
    writer.write(FileRecordKind.FINISHED, empty_file_inventory_body())
    return 0


def main(nonce: str) -> int:
    """Execute one list request after nonce, runtime and exact identity checks."""
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
    except FileInventoryRequestError as error:
        return _finish_failure(writer, error.failure)
    if request.nonce != nonce:
        return _finish_failure(writer, FileInventoryFailureCode.NONCE_MISMATCH)
    if sys.platform != "linux":
        return _finish_failure(writer, FileInventoryFailureCode.UNSUPPORTED_RUNTIME)
    if not matches_current_identity(request.identity):
        return _finish_failure(writer, FileInventoryFailureCode.IDENTITY_MISMATCH)
    try:
        expires_at = _expires_at(request.remaining_seconds)
        with system_file_lock(expires_at=expires_at):
            snapshot = _snapshot(request, expires_at)
            _raise_if_expired(expires_at)
    except FileLockError as error:
        return _finish_failure(writer, _lock_failure(error))
    except _SafeFailure as error:
        return _finish_failure(writer, error.failure)
    if snapshot is None:
        writer.write(FileRecordKind.ABSENT, empty_file_inventory_body())
    else:
        _emit_snapshot(writer, snapshot)
    writer.write(FileRecordKind.FINISHED, empty_file_inventory_body())
    return 0
