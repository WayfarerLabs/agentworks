"""Closed control-body schemas shared by the inline helper and observer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_LOWER_HEX = frozenset("0123456789abcdef")
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class ControlError(ValueError):
    """A control body violated its closed schema without retaining the body."""

    def __init__(self) -> None:
        super().__init__("invalid control body")


class StreamName(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


class StreamRetention(StrEnum):
    CAPTURED = "captured"
    DISCARDED = "discarded"
    SUPPRESSED = "suppressed"


class WaitKind(StrEnum):
    EXIT = "exit"
    SIGNAL = "signal"
    UNKNOWN = "unknown"


class FailurePhase(StrEnum):
    REQUEST = "request"
    IDENTITY = "identity"
    PREPARE = "prepare"
    LAUNCH = "launch"
    OBSERVE = "observe"
    CLEANUP = "cleanup"


class FailureCode(StrEnum):
    INVALID = "invalid"
    OVERSIZED = "oversized"
    NONCE = "nonce"
    MISMATCH = "mismatch"
    ACCOUNT = "account"
    SHELL = "shell"
    RUNTIME = "runtime"
    SOURCE = "source"
    DISPATCH = "dispatch"
    INPUT = "input"
    OUTPUT = "output"
    OBSERVATION = "observation"
    RESOURCE = "resource"


_FAILURE_PAIRS = frozenset(
    {
        (FailurePhase.REQUEST, FailureCode.INVALID),
        (FailurePhase.REQUEST, FailureCode.OVERSIZED),
        (FailurePhase.REQUEST, FailureCode.NONCE),
        (FailurePhase.IDENTITY, FailureCode.MISMATCH),
        (FailurePhase.IDENTITY, FailureCode.ACCOUNT),
        (FailurePhase.IDENTITY, FailureCode.SHELL),
        (FailurePhase.PREPARE, FailureCode.RUNTIME),
        (FailurePhase.PREPARE, FailureCode.SOURCE),
        (FailurePhase.LAUNCH, FailureCode.DISPATCH),
        (FailurePhase.OBSERVE, FailureCode.INPUT),
        (FailurePhase.OBSERVE, FailureCode.OUTPUT),
        (FailurePhase.OBSERVE, FailureCode.OBSERVATION),
        (FailurePhase.CLEANUP, FailureCode.RESOURCE),
    }
)


@dataclass(frozen=True, slots=True)
class StreamEnd:
    stream: StreamName
    retained: int
    sha256: str
    complete: bool
    truncated: bool
    retention: StreamRetention


@dataclass(frozen=True, slots=True)
class WaitFact:
    kind: WaitKind
    value: int | None


@dataclass(frozen=True, slots=True)
class FailureFact:
    phase: FailurePhase
    code: FailureCode


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _object(body: bytes, fields: set[str]) -> dict[str, Any]:
    failed = False
    value: Any = None
    try:
        value = json.loads(body.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        failed = True
    if failed:
        raise ControlError
    if type(value) is not dict or set(value) != fields or _json_bytes(value) != body:
        raise ControlError
    return value


def empty_body() -> bytes:
    return b"{}"


def parse_empty(body: bytes) -> None:
    if body != empty_body():
        raise ControlError


def encode_stream_end(value: StreamEnd) -> bytes:
    return _json_bytes(
        {
            "complete": value.complete,
            "retained": value.retained,
            "retention": value.retention.value,
            "sha256": value.sha256,
            "stream": value.stream.value,
            "truncated": value.truncated,
        }
    )


def parse_stream_end(body: bytes) -> StreamEnd:
    value = _object(body, {"complete", "retained", "retention", "sha256", "stream", "truncated"})
    failed = False
    stream = StreamName.STDOUT
    retention = StreamRetention.CAPTURED
    try:
        stream = StreamName(value["stream"])
        retention = StreamRetention(value["retention"])
    except (TypeError, ValueError):
        failed = True
    if failed:
        raise ControlError
    retained = value["retained"]
    digest = value["sha256"]
    complete = value["complete"]
    truncated = value["truncated"]
    if (
        type(retained) is not int
        or not 0 <= retained <= 4_096
        or type(digest) is not str
        or len(digest) != 64
        or any(character not in _LOWER_HEX for character in digest)
        or type(complete) is not bool
        or type(truncated) is not bool
        or (truncated and complete)
        or (retention is not StreamRetention.CAPTURED and (retained != 0 or digest != _EMPTY_SHA256 or truncated))
    ):
        raise ControlError
    return StreamEnd(stream, retained, digest, complete, truncated, retention)


def encode_wait(value: WaitFact) -> bytes:
    return _json_bytes({"kind": value.kind.value, "value": value.value})


def parse_wait(body: bytes) -> WaitFact:
    value = _object(body, {"kind", "value"})
    failed = False
    kind = WaitKind.UNKNOWN
    try:
        kind = WaitKind(value["kind"])
    except (TypeError, ValueError):
        failed = True
    if failed:
        raise ControlError
    status = value["value"]
    if kind is WaitKind.UNKNOWN:
        if status is not None:
            raise ControlError
    elif type(status) is not int or status < (1 if kind is WaitKind.SIGNAL else 0) or status > 255:
        raise ControlError
    return WaitFact(kind, status)


def encode_failure(value: FailureFact) -> bytes:
    return _json_bytes({"code": value.code.value, "phase": value.phase.value})


def parse_failure(body: bytes) -> FailureFact:
    value = _object(body, {"code", "phase"})
    failed = False
    result = FailureFact(FailurePhase.REQUEST, FailureCode.INVALID)
    try:
        result = FailureFact(FailurePhase(value["phase"]), FailureCode(value["code"]))
    except (TypeError, ValueError):
        failed = True
    if failed:
        raise ControlError
    if (result.phase, result.code) not in _FAILURE_PAIRS:
        raise ControlError
    return result
