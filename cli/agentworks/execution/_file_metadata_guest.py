"""Destination entry point for one metadata operation."""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress

from ._file_effect_gate import (
    FileEffectGateError,
    hold_file_effect_gate,
)
from ._file_metadata import (
    MetadataError,
    MetadataFailureKind,
    MetadataPhase,
    MetadataResult,
    create_directory,
    ensure_directory,
    set_metadata,
)
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
    targets_metadata_gate_ancestor,
)
from ._file_paths import ConfinedOpenError, directory_path_flags, open_file_confined, open_file_root
from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_file_helper_identity
from ._vm_guest_identity_guest import _GuestRefusal, _identity


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
        root_fd = open_file_root(request.root_path)
    except ConfinedOpenError:
        raise _SafeFailure(FileMetadataFailureControl(FileMetadataFailureCode.ROOT_REFUSED)) from None
    if root_fd is None:
        raise _SafeFailure(FileMetadataFailureControl(FileMetadataFailureCode.ROOT_REFUSED))
    components = request.relative_path.split("/")
    leaf = components[-1]
    if len(components) == 1:
        return root_fd, None, leaf
    flags = directory_path_flags()
    try:
        parent_fd = open_file_confined(root_fd, "/".join(components[:-1]), flags)
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
    if (
        sys.platform == "linux"
        and request.operation is not FileMetadataOperation.CREATE_DIRECTORY
        and targets_metadata_gate_ancestor(request.root_path, request.relative_path)
    ):
        raise _SafeFailure(FileMetadataFailureControl(FileMetadataFailureCode.EFFECT_GATE_REFUSED))
    root_fd, parent_fd, leaf = _parent(request)
    descriptor = root_fd if parent_fd is None else parent_fd
    try:
        if request.operation is FileMetadataOperation.SET_METADATA:
            function = set_metadata
        elif request.operation is FileMetadataOperation.ENSURE_DIRECTORY:
            function = ensure_directory
        else:
            function = create_directory
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
    if sys.platform not in {"linux", "darwin"}:
        return _finish_failure(writer, FileMetadataFailureControl(FileMetadataFailureCode.UNSUPPORTED_RUNTIME))
    if not matches_file_helper_identity(request.identity):
        return _finish_failure(writer, FileMetadataFailureControl(FileMetadataFailureCode.IDENTITY_MISMATCH))
    expires_at = None if request.remaining_seconds is None else time.monotonic() + request.remaining_seconds
    if expires_at is not None and time.monotonic() >= expires_at:
        return _finish_failure(
            writer,
            FileMetadataFailureControl(
                FileMetadataFailureCode.METADATA,
                MetadataFailureKind.DEADLINE,
                MetadataPhase.OBSERVATION,
            ),
        )
    failure: FileMetadataFailureControl | None = None
    result: FileMetadataResultControl | None = None
    try:
        if request.effect_gate is None:
            result = _operate(request, expires_at)
        else:
            with hold_file_effect_gate(request.effect_gate, _identity, expires_at=expires_at):
                result = _operate(request, expires_at)
    except _SafeFailure as error:
        failure = error.failure
    except (FileEffectGateError, _GuestRefusal):
        return _finish_failure(writer, FileMetadataFailureControl(FileMetadataFailureCode.EFFECT_GATE_REFUSED))
    if failure is not None:
        return _finish_failure(writer, failure)
    assert result is not None
    writer.write(FileRecordKind.RESULT, encode_file_metadata_result(result))
    writer.write(FileRecordKind.FINISHED, empty_file_metadata_body())
    return 0
