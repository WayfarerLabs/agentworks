"""Shared AGWF1 response records for private file helpers."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_RECORD_BODY_BYTES = 4_096
MAX_RECORD_BYTES = 8_192
_MAX_SEQUENCE = 2**63 - 1
_MARKER = b"AGWF1"
_LOWER_HEX = frozenset("0123456789abcdef")


class FileRecordKind(StrEnum):
    DATA = "DATA"
    RESULT = "RESULT"
    ABSENT = "ABSENT"
    FAILED = "FAILED"
    FINISHED = "FINISHED"


class FileWireError(StrEnum):
    MALFORMED = "malformed"
    OVERSIZED = "oversized"
    NONCE = "nonce"
    SEQUENCE = "sequence"
    TRUNCATED = "truncated"
    CALLBACK = "callback"


@dataclass(frozen=True, slots=True)
class FileRecord:
    sequence: int
    kind: FileRecordKind
    body: bytes = field(repr=False)


def valid_nonce(value: object) -> bool:
    return type(value) is str and len(value) == 32 and all(character in _LOWER_HEX for character in value)


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


def encode_file_record(nonce: str, record: FileRecord) -> bytes:
    if not valid_nonce(nonce) or not 0 <= record.sequence <= _MAX_SEQUENCE or len(record.body) > MAX_RECORD_BODY_BYTES:
        raise ValueError("file record is outside the version-one bounds")
    encoded_body = base64.b64encode(record.body)
    return (
        b" ".join(
            (
                _MARKER,
                nonce.encode("ascii"),
                str(record.sequence).encode("ascii"),
                record.kind.value.encode("ascii"),
                str(len(record.body)).encode("ascii"),
                encoded_body,
            )
        )
        + b"\n"
    )


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
