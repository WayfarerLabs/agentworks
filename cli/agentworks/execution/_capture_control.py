"""Closed remote capture facts, independent from locally retrieved bytes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from ._inline_control import ControlError, StreamEnd, StreamName, StreamRetention, _object
from ._scratch_receipt import ScratchOperation, ScratchReceiptContext
from ._scratch_wire import ScratchWireError, decode_ready_scratch_reference, encode_ready_scratch_reference

if TYPE_CHECKING:
    from ._helper_identity import IdentityExpectation
    from ._scratch import ReadyScratchReference


class CaptureCreation(StrEnum):
    NOT_ENTERED = "not_entered"
    POSSIBLE = "possible"


@dataclass(frozen=True, slots=True, repr=False)
class CaptureStreamFact:
    end: StreamEnd
    creation: CaptureCreation
    ready: ReadyScratchReference | None = None


@dataclass(frozen=True, slots=True, repr=False)
class RetrievedCapture:
    data: bytes = field(repr=False)
    verified: bool = False


def encode_capture_fact(fact: CaptureStreamFact) -> bytes:
    end = fact.end
    capture = (
        {"state": "ready", "ready": encode_ready_scratch_reference(fact.ready)}
        if fact.ready is not None
        else {"state": "unavailable", "creation": fact.creation.value}
    )
    return json.dumps(
        {
            "version": 1,
            "stream": end.stream.value,
            "count": end.count,
            "sha256": end.sha256,
            "complete": end.complete,
            "truncated": end.truncated,
            "retention": end.retention.value,
            "capture": capture,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def parse_capture_fact(
    body: bytes, tokens: tuple[bytes, bytes], identity: IdentityExpectation, limit: int
) -> CaptureStreamFact:
    """Admit one untrusted remote record under the exact original capture binding."""
    try:
        value = _object(body, {"version", "stream", "count", "sha256", "complete", "truncated", "retention", "capture"})
        if type(value["version"]) is not int or value["version"] != 1:
            raise ControlError
        stream = StreamName(value["stream"])
        count, digest = value["count"], value["sha256"]
        complete, truncated = value["complete"], value["truncated"]
        if (
            type(count) is not int
            or not 0 <= count <= limit
            or type(digest) is not str
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or type(complete) is not bool
            or type(truncated) is not bool
            or (truncated and (complete or count != limit))
            or value["retention"] != StreamRetention.CAPTURED.value
            or (count == 0 and digest != hashlib.sha256(b"").hexdigest())
        ):
            raise ControlError
        end = StreamEnd(stream, count, digest, complete, truncated, StreamRetention.CAPTURED)
        capture = value["capture"]
        if type(capture) is not dict:
            raise ControlError
        if set(capture) == {"state", "ready"} and capture["state"] == "ready":
            token = tokens[0 if stream is StreamName.STDOUT else 1]
            ready = decode_ready_scratch_reference(
                capture["ready"], token, ScratchReceiptContext(ScratchOperation.SNAPSHOT, identity)
            )
            if ready._reference._ownership._length != count or ready._digest.hex() != digest:
                raise ControlError
            return CaptureStreamFact(end, CaptureCreation.POSSIBLE, ready)
        if set(capture) != {"state", "creation"} or capture["state"] != "unavailable":
            raise ControlError
        return CaptureStreamFact(end, CaptureCreation(capture["creation"]))
    except (ValueError, TypeError, KeyError, ScratchWireError):
        raise ControlError from None
