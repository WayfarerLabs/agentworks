"""Fixed root guest clock observation and exact run lease publisher."""

from __future__ import annotations

import os
import sys

from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity
from ._managed_job_store import FactName, ManagedJobStore, StoreError
from ._managed_lease_protocol import (
    MAX_REQUEST_BYTES,
    ClockObservation,
    LeasePublication,
    LeaseRequest,
    decode_request,
    encode_result,
)
from ._managed_lease_store import publish_lease
from ._managed_lease_wire import LeaseError, boottime_ns, checked_lease
from ._vm_guest_identity_guest import _GuestRefusal, _identity


def _read_request() -> LeaseRequest:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return decode_request(bytes(data))


def _publish(request: LeaseRequest, store: ManagedJobStore) -> LeasePublication:
    assert request.lease is not None and request.expected_launch is not None
    if store.read_fact(FactName.LAUNCH) != request.expected_launch:
        raise LeaseError("lease launch binding mismatch")
    checked_lease(request.lease, request.expected_launch, boottime_ns())
    publish_lease(store, request.expected_launch, request.lease)
    return LeasePublication(request.lease.expires_ns)


def main(nonce: str) -> int:
    writer = FileRecordWriter(nonce)
    try:
        request = _read_request()
        if request.nonce != nonce or sys.platform != "linux" or not matches_current_identity(request.identity):
            raise LeaseError("lease prerequisite")
        if _identity() != request.guest:
            raise LeaseError("lease guest identity mismatch")
        result: ClockObservation | LeasePublication
        if request.lease is None:
            result = ClockObservation(boottime_ns())
        else:
            with ManagedJobStore(request.lease.run_id) as store:
                result = _publish(request, store)
    except (LeaseError, StoreError, _GuestRefusal, OSError, ValueError, TypeError):
        writer.write(FileRecordKind.FAILED, b"")
        writer.write(FileRecordKind.FINISHED, b"")
        return 0
    writer.write(FileRecordKind.RESULT, encode_result(result))
    writer.write(FileRecordKind.FINISHED, b"")
    return 0
