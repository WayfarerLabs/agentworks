"""Host-side parser for sequenced AGWF1 helper responses."""

from __future__ import annotations

import base64
import binascii
from enum import StrEnum
from typing import TYPE_CHECKING

from ._file_wire import MAX_RECORD_BODY_BYTES, MAX_RECORD_BYTES, FileRecord, FileRecordKind, valid_nonce

if TYPE_CHECKING:
    from collections.abc import Callable

_MAX_SEQUENCE = 2**63 - 1
_MARKER = b"AGWF1"


class FileWireError(StrEnum):
    MALFORMED = "malformed"
    OVERSIZED = "oversized"
    NONCE = "nonce"
    SEQUENCE = "sequence"
    TRUNCATED = "truncated"
    CALLBACK = "callback"


def _parse_uint(token: bytes, maximum: int) -> int | None:
    if (
        not token
        or (len(token) > 1 and token[0] == ord("0"))
        or any(not ord("0") <= byte <= ord("9") for byte in token)
    ):
        return None
    maximum_token = str(maximum).encode("ascii")
    if len(token) > len(maximum_token) or (len(token) == len(maximum_token) and token > maximum_token):
        return None
    return int(token)


class FileRecordReader:
    """Strictly consume one nonce's records without retaining rejected bytes."""

    def __init__(self, nonce: str, on_record: Callable[[FileRecord], None]) -> None:
        if not valid_nonce(nonce):
            raise ValueError("file nonce must be 32 lowercase hexadecimal characters")
        self._nonce = nonce.encode("ascii")
        self._on_record = on_record
        self._record = bytearray()
        self._next_sequence = 0
        self._error: FileWireError | None = None
        self._finished = False

    @property
    def error(self) -> FileWireError | None:
        return self._error

    def try_write(self, data: memoryview) -> int:
        """Consume at most one maximum record while applying sink backpressure."""
        consumed = min(len(data), MAX_RECORD_BYTES)
        if self._finished or self._error is not None:
            return consumed
        for byte in data[:consumed]:
            if len(self._record) == MAX_RECORD_BYTES:
                self._fail(FileWireError.OVERSIZED)
                break
            self._record.append(byte)
            if byte == ord("\n"):
                record = bytes(self._record)
                self._record.clear()
                self._accept_record(record)
                if self._error is not None:
                    break
        return consumed

    def finish(self) -> None:
        if not self._finished and self._error is None and self._record:
            self._fail(FileWireError.TRUNCATED)
        self._finished = True

    def abort(self) -> None:
        """Forget borrowed stream bytes when the attempt cannot return a result."""
        self._record.clear()
        self._finished = True

    def _accept_record(self, record: bytes) -> None:
        fields = record[:-1].split(b" ")
        if len(fields) != 6 or fields[0] != _MARKER:
            self._fail(FileWireError.MALFORMED)
            return
        if fields[1] != self._nonce:
            self._fail(FileWireError.NONCE)
            return
        sequence = _parse_uint(fields[2], _MAX_SEQUENCE)
        decoded_length = _parse_uint(fields[4], MAX_RECORD_BODY_BYTES)
        failed = False
        body = b""
        try:
            kind = FileRecordKind(fields[3].decode("ascii"))
            body = base64.b64decode(fields[5], validate=True)
        except (UnicodeDecodeError, ValueError, binascii.Error):
            failed = True
            kind = FileRecordKind.FINISHED
        if (
            failed
            or sequence is None
            or decoded_length is None
            or len(body) != decoded_length
            or base64.b64encode(body) != fields[5]
        ):
            self._fail(FileWireError.MALFORMED)
            return
        if sequence != self._next_sequence:
            self._fail(FileWireError.SEQUENCE)
            return
        self._next_sequence += 1
        try:
            self._on_record(FileRecord(sequence, kind, body))
        except Exception:
            self._fail(FileWireError.CALLBACK)
            raise

    def _fail(self, error: FileWireError) -> None:
        if self._error is None:
            self._error = error
        self._record.clear()
