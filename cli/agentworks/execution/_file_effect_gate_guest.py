"""Fixed Linux guest entry point for private file-gate control."""

from __future__ import annotations

import os
import sys
import time

from ._file_effect_gate import (
    FileEffectGateBinding,
    FileEffectGateError,
    advance_file_effect_gate,
    inspect_file_effect_gate,
    setup_file_effect_gate,
)
from ._file_effect_gate_protocol import (
    MAX_REQUEST_BYTES,
    GateControlFailure,
    GateControlOperation,
    GateControlProtocolError,
    GateControlRequest,
    decode_gate_control_request,
    encode_gate_control_failure,
    encode_gate_control_result,
)
from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity
from ._vm_guest_identity_guest import _GuestRefusal, _identity


class _GuestMismatch(Exception):
    pass


def _read_request() -> bytes:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _fail(writer: FileRecordWriter, code: GateControlFailure) -> int:
    writer.write(FileRecordKind.FAILED, encode_gate_control_failure(code))
    writer.write(FileRecordKind.FINISHED, b"{}")
    return 0


def _operate(request: GateControlRequest, expires_at: float | None) -> FileEffectGateBinding:
    if expires_at is not None and time.monotonic() >= expires_at:
        raise TimeoutError
    # This observation is independent of the host's earlier target preparation.
    # The primitive observes again before its own filesystem or database effect.
    if _identity() != request.guest:
        raise _GuestMismatch
    if expires_at is not None and time.monotonic() >= expires_at:
        raise TimeoutError
    if request.operation is GateControlOperation.SETUP:
        return setup_file_effect_gate(
            request.path, request.guest, request.euid, request.scope_name, _identity, expires_at=expires_at
        )
    if request.operation is GateControlOperation.INSPECT:
        return inspect_file_effect_gate(
            request.path, request.guest, request.euid, request.scope_name, _identity, expires_at=expires_at
        )
    assert request.binding is not None
    return advance_file_effect_gate(request.binding, _identity, expires_at=expires_at)


def main(nonce: str) -> int:
    """Execute one control request, emitting only sequenced closed records."""
    writer = FileRecordWriter(nonce)
    data = _read_request()
    if len(data) > MAX_REQUEST_BYTES:
        return _fail(writer, GateControlFailure.OVERSIZED_REQUEST)
    try:
        request = decode_gate_control_request(data)
    except GateControlProtocolError:
        return _fail(writer, GateControlFailure.INVALID_REQUEST)
    if request.nonce != nonce:
        return _fail(writer, GateControlFailure.NONCE_MISMATCH)
    if sys.platform != "linux":
        return _fail(writer, GateControlFailure.UNSUPPORTED_RUNTIME)
    if not matches_current_identity(request.identity):
        return _fail(writer, GateControlFailure.IDENTITY_MISMATCH)
    expires_at = None if request.remaining_seconds is None else time.monotonic() + request.remaining_seconds
    try:
        binding = _operate(request, expires_at)
    except TimeoutError:
        return _fail(writer, GateControlFailure.DEADLINE)
    except (_GuestRefusal, _GuestMismatch):
        return _fail(writer, GateControlFailure.GUEST_MISMATCH)
    except FileEffectGateError:
        return _fail(writer, GateControlFailure.GATE)
    writer.write(FileRecordKind.RESULT, encode_gate_control_result(binding))
    writer.write(FileRecordKind.FINISHED, b"{}")
    return 0
