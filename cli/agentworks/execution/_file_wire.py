"""Shared AGWF1 response records for private file helpers."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from enum import StrEnum

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


@dataclass(frozen=True, slots=True)
class FileRecord:
    sequence: int
    kind: FileRecordKind
    body: bytes = field(repr=False)


def valid_nonce(value: object) -> bool:
    return type(value) is str and len(value) == 32 and all(character in _LOWER_HEX for character in value)


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


class FileRecordWriter:
    """Write one helper's sequenced AGWF1 records completely to stdout."""

    def __init__(self, nonce: str) -> None:
        self._nonce = nonce
        self._sequence = 0

    def write(self, kind: FileRecordKind, body: bytes) -> None:
        remaining = memoryview(encode_file_record(self._nonce, FileRecord(self._sequence, kind, body)))
        while remaining:
            written = os.write(1, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        self._sequence += 1
