"""Bounded stdlib-only framing for private application evidence."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_MARKER = b"AGWE1 "
_MAX_BODY_BYTES = 4_096
_MAX_RECORD_BYTES = 8_192
_MAX_SEQUENCE = 2**63 - 1
_LOWER_HEX = frozenset("0123456789abcdef")


class FrameKind(StrEnum):
    LAUNCHING = "LAUNCHING"
    STARTED = "STARTED"
    STDOUT = "STDOUT"
    STDERR = "STDERR"
    STREAM_END = "STREAM_END"
    WAITED = "WAITED"
    FAILED = "FAILED"
    FINISHED = "FINISHED"


@dataclass(frozen=True, slots=True)
class Frame:
    sequence: int
    kind: FrameKind
    body: bytes = field(repr=False)


class WireError(StrEnum):
    MALFORMED = "malformed"
    OVERSIZED = "oversized"
    SEQUENCE = "sequence"
    TRUNCATED = "truncated"
    CALLBACK = "callback"


def _nonce_bytes(nonce: str) -> bytes:
    if type(nonce) is not str or len(nonce) != 32 or any(character not in _LOWER_HEX for character in nonce):
        raise ValueError("evidence nonce must be 32 lowercase hexadecimal characters")
    return nonce.encode("ascii")


def encode_frame(nonce: str, frame: Frame) -> bytes:
    """Encode one canonical record from trusted helper state."""
    nonce_bytes = _nonce_bytes(nonce)
    if (
        not isinstance(frame, Frame)
        or type(frame.sequence) is not int
        or not 0 <= frame.sequence <= _MAX_SEQUENCE
        or not isinstance(frame.kind, FrameKind)
        or type(frame.body) is not bytes
        or len(frame.body) > _MAX_BODY_BYTES
    ):
        raise ValueError("evidence frame is outside the version-one bounds")
    encoded_body = base64.b64encode(frame.body)
    record = (
        b" ".join(
            (
                _MARKER.rstrip(),
                nonce_bytes,
                str(frame.sequence).encode("ascii"),
                frame.kind.value.encode("ascii"),
                str(len(frame.body)).encode("ascii"),
                encoded_body,
            )
        )
        + b"\n"
    )
    if len(record) > _MAX_RECORD_BYTES:
        raise ValueError("evidence frame is outside the version-one bounds")
    return record


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


class FrameReader:
    """Consume one nonce's records; the first-party callback does no external I/O."""

    def __init__(self, nonce: str, on_frame: Callable[[Frame], None]) -> None:
        nonce_bytes = _nonce_bytes(nonce)
        self._own_tag = _MARKER + nonce_bytes
        self._on_frame = on_frame
        self._matched = 0
        self._record: bytearray | None = None
        self._next_sequence: int | None = 0
        self._error: WireError | None = None
        self._finished = False

    @property
    def error(self) -> WireError | None:
        return self._error

    def try_write(self, data: memoryview) -> int:
        """Consume at most one maximum-sized record's worth of borrowed bytes."""
        consumed = min(len(data), _MAX_RECORD_BYTES)
        if self._finished or self._error is not None:
            return consumed
        for byte in data[:consumed]:
            if self._record is None:
                self._seek_own_tag(byte)
            else:
                self._append_record_byte(byte)
            if self._error is not None:
                break
        return consumed

    def finish(self) -> None:
        """Close input and latch an identified partial record without interpreting it."""
        if not self._finished and self._error is None and self._record is not None:
            self._error = WireError.TRUNCATED
            self._record = None
        self._finished = True

    def _seek_own_tag(self, byte: int) -> None:
        if byte == self._own_tag[self._matched]:
            self._matched += 1
            if self._matched == len(self._own_tag):
                self._record = bytearray(self._own_tag)
                self._matched = 0
        elif byte == self._own_tag[0]:
            self._matched = 1
        else:
            self._matched = 0

    def _append_record_byte(self, byte: int) -> None:
        assert self._record is not None
        if len(self._record) == _MAX_RECORD_BYTES:
            self._fail(WireError.OVERSIZED)
            return
        self._record.append(byte)
        if byte == ord("\n"):
            record = bytes(self._record)
            self._record = None
            self._accept_record(record)

    def _accept_record(self, record: bytes) -> None:
        fields = record[:-1].split(b" ")
        if len(fields) != 6 or fields[0] != _MARKER.rstrip() or fields[1] != self._own_tag[len(_MARKER) :]:
            self._fail(WireError.MALFORMED)
            return
        sequence = _parse_uint(fields[2], _MAX_SEQUENCE)
        decoded_length = _parse_uint(fields[4], _MAX_BODY_BYTES)
        try:
            kind = FrameKind(fields[3].decode("ascii"))
            body = base64.b64decode(fields[5], validate=True)
        except (UnicodeDecodeError, ValueError, binascii.Error):
            self._fail(WireError.MALFORMED)
            return
        if (
            sequence is None
            or decoded_length is None
            or len(body) != decoded_length
            or base64.b64encode(body) != fields[5]
        ):
            self._fail(WireError.MALFORMED)
            return
        if self._next_sequence is None or sequence != self._next_sequence:
            self._fail(WireError.SEQUENCE)
            return
        self._next_sequence = None if sequence == _MAX_SEQUENCE else sequence + 1
        try:
            self._on_frame(Frame(sequence, kind, body))
        except BaseException:
            self._fail(WireError.CALLBACK)
            raise

    def _fail(self, error: WireError) -> None:
        if self._error is None:
            self._error = error
        self._record = None
        self._matched = 0
