"""Fixed Linux root helper for one exact managed-run disposal."""

from __future__ import annotations

import os
import sys
from typing import cast

from ._file_wire import FileRecordKind, FileRecordWriter
from ._helper_identity import matches_current_identity
from ._managed_disposal_protocol import MAX_REQUEST_BYTES, DisposalError, DisposalResult, decode_request, encode_result
from ._managed_job_store import ManagedJobStore, StoreError
from ._managed_observation_protocol import checked_launch


def _read_request() -> bytes:
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = os.read(0, MAX_REQUEST_BYTES + 1 - len(data))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise DisposalError("oversize disposal request")


def main(nonce: str) -> int:
    writer = FileRecordWriter(nonce)
    try:
        request = decode_request(_read_request())
        if request.nonce != nonce or sys.platform != "linux" or not matches_current_identity(request.identity):
            raise DisposalError("disposal prerequisite")
        launch = checked_launch(request.expected_launch)
        with ManagedJobStore(cast("str", launch["run_id"])) as store:
            disposed = store.dispose(request.expected_launch)
    except (DisposalError, StoreError, OSError, ValueError):
        writer.write(FileRecordKind.FAILED, b"")
        writer.write(FileRecordKind.FINISHED, b"")
        return 0
    writer.write(
        FileRecordKind.RESULT, encode_result(DisposalResult.DISPOSED if disposed else DisposalResult.NOT_READY)
    )
    if disposed:
        writer.write(FileRecordKind.DATA, request.expected_launch)
    writer.write(FileRecordKind.FINISHED, b"")
    return 0
