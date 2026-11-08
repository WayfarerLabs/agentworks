"""Buffered invocation boundary for independent carrier implementations.

Reports describe channel observations, not proof that a bootstrap or application ran.
Payload fields deliberately have no diagnostic representation. No type imports
the legacy execution stack, resolves credentials, or opens a connection.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from agentworks.errors import ValidationError


@dataclass(frozen=True)
class Deadline:
    """One local observation budget, not guest cancellation; None is unbounded."""

    expires_at: float | None

    def __post_init__(self) -> None:
        if self.expires_at is not None and not math.isfinite(self.expires_at):
            raise ValidationError("Deadline must be finite or explicitly unbounded")

    @classmethod
    def after(cls, seconds: float | None) -> Deadline:
        if seconds is None:
            return cls(None)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValidationError("Deadline duration must be finite and nonnegative")
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.monotonic())

    @property
    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0


@dataclass(frozen=True)
class ChannelFeatures:
    """Passive channel properties, not health or authorization."""

    live_stdio: bool = False
    terminal: bool = False


@dataclass(frozen=True)
class PreparedInvocation:
    """Literal bootstrap argv supplied by trusted execution preparation.

    Adapter-author callers can be outside static typing, so validate argv here.
    """

    argv: tuple[str, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.argv, tuple)
            or not self.argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in self.argv)
            or not self.argv[0]
        ):
            raise ValidationError("Prepared invocation requires literal non-NUL argv")


@dataclass(frozen=True)
class EndOfInput:
    """Close the carrier-owned input channel; never inherit workstation stdin."""


@dataclass(frozen=True)
class FiniteInput:
    """Immutable finite source; a carrier sends these bytes once, then EOF."""

    data: bytes = field(repr=False)
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise ValidationError("Finite input must be bytes")


@dataclass(frozen=True)
class Capture:
    """Maximum retained bytes per carrier stream, not a truncation permission."""

    max_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if type(self.max_bytes) is not int or self.max_bytes < 0:
            raise ValidationError("Capture bound must be a nonnegative integer")


@dataclass(frozen=True)
class Discard:
    """Observe execution without retaining stream contents."""


@dataclass(frozen=True)
class CarrierIO:
    """The sole input owner for the buffered proof subset of the carrier API.

    Live streams and terminals are deliberately not accepted by this slice.
    They require a proven cancellation/ownership contract before implementation.
    """

    input: EndOfInput | FiniteInput = field(default_factory=EndOfInput)
    output: Capture | Discard = field(default_factory=Capture)
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.input, EndOfInput | FiniteInput) or not isinstance(self.output, Capture | Discard):
            raise ValidationError("The buffered carrier requires finite input and capture or discard output")
        if isinstance(self.input, FiniteInput) and self.input.sensitive:
            object.__setattr__(self, "sensitive", True)


class Dispatch(StrEnum):
    NOT_SENT = "not_sent"
    SENT = "sent"
    UNKNOWN = "unknown"


class Provenance(StrEnum):
    UNKNOWN = "unknown"
    CARRIER_STDOUT = "carrier_stdout"
    MIXED_STDERR = "mixed_stderr"


class Retention(StrEnum):
    CAPTURED = "captured"
    DISCARDED = "discarded"
    SUPPRESSED = "suppressed"


class Failure(StrEnum):
    DEADLINE = "deadline"
    DISPATCH = "dispatch"
    OBSERVATION = "observation"
    INVALID_RESPONSE = "invalid_response"
    INPUT = "input"
    OUTPUT = "output"
    OUTPUT_LIMIT = "output_limit"


@dataclass(frozen=True)
class ExitStatus:
    """Observed remote command-chain exit or signal, possibly before bootstrap."""

    code: int | None = None
    signal: int | None = None

    def __post_init__(self) -> None:
        if (self.code is None) == (self.signal is None):
            raise ValidationError("Completion requires exactly one exit code or signal")
        if self.code is not None and (type(self.code) is not int or self.code < 0):
            raise ValidationError("Exit code must be a nonnegative integer")
        if self.signal is not None and (type(self.signal) is not int or self.signal <= 0):
            raise ValidationError("Signal must be a positive integer")


@dataclass(frozen=True)
class CapturedOutput:
    """Retained raw carrier bytes with independent completeness and provenance."""

    data: bytes = field(default=b"", repr=False)
    complete: bool = False
    provenance: Provenance = Provenance.UNKNOWN
    retention: Retention = Retention.CAPTURED

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise ValidationError("Carrier output must be bytes")
        if self.retention != Retention.CAPTURED and self.data:
            raise ValidationError("Unretained output cannot contain bytes")


@dataclass(frozen=True)
class CarrierReport:
    """Safe evidence from one attempt, without provider exception text."""

    dispatch: Dispatch
    completion: ExitStatus | None = None
    local_status: int | None = None
    stdout: CapturedOutput = field(default_factory=CapturedOutput)
    stderr: CapturedOutput = field(default_factory=CapturedOutput)
    failure: Failure | None = None

    def __post_init__(self) -> None:
        if self.completion is not None and self.dispatch != Dispatch.SENT:
            raise ValidationError("Observed completion requires submitted dispatch evidence")


class Carrier(Protocol):
    """Dispatch at most once; clean local resources before return or interrupt."""

    @property
    def features(self) -> ChannelFeatures: ...

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport: ...
