"""Fixed Linux root helper for durable exact-run stop intent."""

from __future__ import annotations

import os
import sys
import time
from typing import cast

from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity
from ._managed_job_store import FactName, ManagedJobStore, StoreError
from ._managed_observation_protocol import ManagedObservationError, checked_fact, checked_launch
from ._managed_stop_protocol import (
    MAX_REQUEST_BYTES,
    ManagedStopError,
    ManagedStopRequest,
    ManagedStopResult,
    decode_request,
    encode_result,
)


def _read_request() -> ManagedStopRequest:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_request(bytes(data))


def _prepare(request: ManagedStopRequest, store: ManagedJobStore) -> tuple[ManagedStopResult, tuple[bytes, ...]]:
    """Publish intent before acknowledging, then observe only boundary emptiness."""
    launch = checked_launch(request.expected_launch)
    if store.run_id != launch["run_id"]:
        raise ManagedStopError("managed store run mismatch")
    store.publish_stop_request(request.expected_launch)
    until = time.monotonic() + request.observation_ms / 1000
    while True:
        boundary = store.read_fact(FactName.BOUNDARY_EMPTY)
        if boundary is not None:
            checked_fact(FactName.BOUNDARY_EMPTY, boundary, request.expected_launch)
            return ManagedStopResult((FactName.LAUNCH, FactName.BOUNDARY_EMPTY)), (request.expected_launch, boundary)
        remaining = until - time.monotonic()
        if remaining <= 0:
            return ManagedStopResult((FactName.LAUNCH,)), (request.expected_launch,)
        time.sleep(min(0.05, remaining))


def main(nonce: str) -> int:
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
        if request.nonce != nonce or sys.platform != "linux" or not matches_current_identity(request.identity):
            raise ManagedStopError("managed stop prerequisite")
        launch = checked_launch(request.expected_launch)
        with ManagedJobStore(cast("str", launch["run_id"])) as store:
            result, facts = _prepare(request, store)
    except (ManagedStopError, ManagedObservationError, StoreError, OSError, ValueError):
        writer.write(FileRecordKind.FAILED, b"")
        writer.write(FileRecordKind.FINISHED, b"")
        return 0
    writer.write(FileRecordKind.RESULT, encode_result(result))
    for fact in facts:
        writer.write(FileRecordKind.DATA, fact)
    writer.write(FileRecordKind.FINISHED, b"")
    return 0
