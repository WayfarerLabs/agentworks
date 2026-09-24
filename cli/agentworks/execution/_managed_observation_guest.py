"""Fixed Linux guest reader for one independent managed run's closed evidence."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import cast

from . import _managed_job_wire as wire
from ._file_wire import MAX_RECORD_BODY_BYTES, FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity
from ._managed_job_store import FactName, ManagedJobStore, StoreError
from ._managed_observation_protocol import (
    FACT_ORDER,
    MAX_REQUEST_BYTES,
    ManagedObservationError,
    ManagedObservationRequest,
    ManagedOperation,
    ManagedResultControl,
    ManagedResultStatus,
    checked_fact,
    checked_launch,
    decode_request,
    encode_result,
)


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedResult:
    control: ManagedResultControl
    facts: tuple[bytes, ...]
    output: bytes = field(default=b"", repr=False)


def _read_request() -> ManagedObservationRequest:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_request(bytes(data))


def _prepare(request: ManagedObservationRequest, store: ManagedJobStore) -> _PreparedResult:
    """Validate all store evidence before emitting a successful response."""
    expected = checked_launch(request.expected_launch)
    if store.run_id != expected["run_id"]:
        raise ManagedObservationError("managed store run mismatch")
    launch = store.read_fact(FactName.LAUNCH)
    if launch is None:
        raise ManagedObservationError("managed launch unavailable")
    checked_fact(FactName.LAUNCH, launch, request.expected_launch)
    if request.operation is ManagedOperation.OBSERVE:
        observed: list[tuple[FactName, bytes]] = [(FactName.LAUNCH, launch)]
        for name in FACT_ORDER[1:]:
            data = store.read_fact(name)
            if data is not None:
                checked_fact(name, data, launch)
                observed.append((name, data))
        return _PreparedResult(
            ManagedResultControl(ManagedResultStatus.OBSERVED, tuple(name for name, _ in observed)),
            tuple(data for _, data in observed),
        )
    assert request.stream is not None
    end_name = FactName.STDOUT_END if request.stream.value == "stdout" else FactName.STDERR_END
    end = store.read_fact(end_name)
    if end is None:
        return _PreparedResult(ManagedResultControl(ManagedResultStatus.UNKNOWN, (FactName.LAUNCH,)), (launch,))
    end_fact = checked_fact(end_name, end, launch)
    output = store.read_capture(request.stream, launch)
    if end_fact["disposition"] in ("discarded", "sensitivity-suppressed"):
        if output is not None:
            raise ManagedObservationError("invalid noncapture output")
        return _PreparedResult(
            ManagedResultControl(ManagedResultStatus.UNAVAILABLE, (FactName.LAUNCH, end_name)), (launch, end)
        )
    if output is None or len(output) > wire.MAX_CAPTURE_PREFIX_BYTES_V1:
        raise ManagedObservationError("invalid closed capture")
    return _PreparedResult(
        ManagedResultControl(ManagedResultStatus.AVAILABLE, (FactName.LAUNCH, end_name), len(output)),
        (launch, end),
        output,
    )


def _write_result(writer: FileRecordWriter, prepared: _PreparedResult) -> None:
    writer.write(FileRecordKind.RESULT, encode_result(prepared.control))
    for fact in prepared.facts:
        writer.write(FileRecordKind.DATA, fact)
    for offset in range(0, len(prepared.output), MAX_RECORD_BODY_BYTES):
        writer.write(FileRecordKind.DATA, prepared.output[offset : offset + MAX_RECORD_BODY_BYTES])
    writer.write(FileRecordKind.FINISHED, b"")


def main(nonce: str) -> int:
    """Observe one exact run; no live marker or boot reread is claimed here."""
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
        if request.nonce != nonce or sys.platform != "linux" or request.identity.euid != 0:
            raise ManagedObservationError("managed observation prerequisite")
        if not matches_current_identity(request.identity):
            raise ManagedObservationError("managed observation identity")
        launch = checked_launch(request.expected_launch)
        with ManagedJobStore(cast("str", launch["run_id"])) as store:
            prepared = _prepare(request, store)
    except (ManagedObservationError, StoreError, OSError, ValueError):
        writer.write(FileRecordKind.FAILED, b"")
        writer.write(FileRecordKind.FINISHED, b"")
        return 0
    _write_result(writer, prepared)
    return 0
