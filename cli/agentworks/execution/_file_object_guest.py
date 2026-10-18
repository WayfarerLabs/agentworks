"""Destination entry point for one file-object operation."""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress

from ._file_effect_gate import FileEffectGateError, hold_file_effect_gate
from ._file_object_protocol import (
    MAX_REQUEST_BYTES,
    FileObjectFailureCode,
    FileObjectFailureControl,
    FileObjectOperation,
    FileObjectRequest,
    FileObjectRequestError,
    FileObjectResultControl,
    FileObjectResultKind,
    decode_file_object_request,
    empty_file_object_body,
    encode_file_object_failure,
    encode_file_object_result,
)
from ._file_objects import (
    FileObjectError,
    FileObjectFailureKind,
    FileObjectPhase,
    remove_file_object,
    revision_kind,
    stat_file_object,
)
from ._file_paths import ConfinedOpenError, directory_path_flags, open_file_confined, open_file_root
from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_file_helper_identity
from ._vm_guest_identity_guest import _GuestRefusal, _identity


class _SafeFailure(Exception):
    def __init__(self, failure: FileObjectFailureControl) -> None:
        self.failure = failure
        super().__init__(failure.code.value)


def _read_request() -> FileObjectRequest:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_file_object_request(bytes(data))


def _parent(request: FileObjectRequest) -> tuple[int | None, int | None, str]:
    try:
        root_fd = open_file_root(request.root_path)
    except ConfinedOpenError:
        raise _SafeFailure(FileObjectFailureControl(FileObjectFailureCode.ROOT_REFUSED)) from None
    components = request.relative_path.split("/")
    leaf = components[-1]
    if root_fd is None or len(components) == 1:
        return root_fd, None, leaf
    flags = directory_path_flags()
    try:
        parent_fd = open_file_confined(root_fd, "/".join(components[:-1]), flags)
    except ConfinedOpenError:
        failure: BaseException = _SafeFailure(FileObjectFailureControl(FileObjectFailureCode.PARENT_REFUSED))
    except BaseException as error:
        failure = error
    else:
        return root_fd, parent_fd, leaf
    with suppress(OSError):
        os.close(root_fd)
    raise failure


def _absence(request: FileObjectRequest, expires_at: float | None) -> FileObjectResultControl:
    if expires_at is not None and time.monotonic() >= expires_at:
        phase = (
            FileObjectPhase.OBSERVATION if request.operation is FileObjectOperation.STAT else FileObjectPhase.CONDITION
        )
        raise _SafeFailure(
            FileObjectFailureControl(FileObjectFailureCode.OBJECT, FileObjectFailureKind.DEADLINE, phase)
        )
    return FileObjectResultControl(
        FileObjectResultKind.ABSENT if request.operation is FileObjectOperation.STAT else FileObjectResultKind.UNCHANGED
    )


def _operate(request: FileObjectRequest, expires_at: float | None) -> FileObjectResultControl:
    root_fd, parent_fd, leaf = _parent(request)
    if root_fd is None:
        return _absence(request, expires_at)
    if len(request.relative_path.split("/")) > 1 and parent_fd is None:
        with suppress(OSError):
            os.close(root_fd)
        return _absence(request, expires_at)
    descriptor = root_fd if parent_fd is None else parent_fd
    assert descriptor is not None
    try:
        if request.operation is FileObjectOperation.STAT:
            revision = stat_file_object(descriptor, leaf, expires_at=expires_at)
            if revision is None:
                return FileObjectResultControl(FileObjectResultKind.ABSENT)
            return FileObjectResultControl(FileObjectResultKind.PRESENT, revision_kind(revision), revision)
        assert request.expected_kind is not None and request.expected_revision is not None
        changed = remove_file_object(
            descriptor,
            leaf,
            expected_kind=request.expected_kind,
            expected=request.expected_revision,
            expires_at=expires_at,
        )
        return FileObjectResultControl(FileObjectResultKind.CHANGED if changed else FileObjectResultKind.UNCHANGED)
    except FileObjectError as error:
        raise _SafeFailure(FileObjectFailureControl(FileObjectFailureCode.OBJECT, error.kind, error.phase)) from None
    finally:
        if parent_fd is not None:
            with suppress(OSError):
                os.close(parent_fd)
        with suppress(OSError):
            os.close(root_fd)


def _finish_failure(writer: FileRecordWriter, failure: FileObjectFailureControl) -> int:
    writer.write(FileRecordKind.FAILED, encode_file_object_failure(failure))
    writer.write(FileRecordKind.FINISHED, empty_file_object_body())
    return 0


def main(nonce: str) -> int:
    """Execute one request after nonce, runtime and exact identity checks."""
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
    except FileObjectRequestError as error:
        return _finish_failure(writer, FileObjectFailureControl(error.failure))
    if request.nonce != nonce:
        return _finish_failure(writer, FileObjectFailureControl(FileObjectFailureCode.NONCE_MISMATCH))
    if sys.platform not in {"linux", "darwin"}:
        return _finish_failure(writer, FileObjectFailureControl(FileObjectFailureCode.UNSUPPORTED_RUNTIME))
    if not matches_file_helper_identity(request.identity):
        return _finish_failure(writer, FileObjectFailureControl(FileObjectFailureCode.IDENTITY_MISMATCH))
    expires_at = None if request.remaining_seconds is None else time.monotonic() + request.remaining_seconds
    if expires_at is not None and time.monotonic() >= expires_at:
        phase = FileObjectPhase.CONDITION
        if request.operation is FileObjectOperation.STAT:
            phase = FileObjectPhase.OBSERVATION
        return _finish_failure(
            writer,
            FileObjectFailureControl(FileObjectFailureCode.OBJECT, FileObjectFailureKind.DEADLINE, phase),
        )
    try:
        if request.effect_gate is None:
            result = _operate(request, expires_at)
        else:
            with hold_file_effect_gate(request.effect_gate, _identity, expires_at=expires_at):
                result = _operate(request, expires_at)
    except _SafeFailure as error:
        return _finish_failure(writer, error.failure)
    except (FileEffectGateError, _GuestRefusal):
        return _finish_failure(writer, FileObjectFailureControl(FileObjectFailureCode.EFFECT_GATE_REFUSED))
    writer.write(FileRecordKind.RESULT, encode_file_object_result(result))
    writer.write(FileRecordKind.FINISHED, empty_file_object_body())
    return 0
