"""Private safe values for job observation and explicit control."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from .carrier import Retention

if TYPE_CHECKING:
    from .models import JobRef
    from .result import ApplicationState, ApplicationStatus, ExecutionFailure


class JobStream(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"


@dataclass(frozen=True, slots=True)
class JobStatus:
    """Application precision and this run's resource closure.

    Closure requires exact launch, both closed streams, an empty workload
    boundary and independent controller termination. It does not settle the
    keeper, original launch debt or other obligations of the operation owner.
    """

    reference: JobRef
    application_state: ApplicationState
    status: ApplicationStatus | None = None
    failure: ExecutionFailure | None = None
    workload_cleanup_confirmed: bool = False
    deadline_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class JobOutput:
    """A slice of verified closed output; EOF need not mean complete capture."""

    reference: JobRef
    stream: JobStream
    data: bytes = field(default=b"", repr=False)
    cursor: int = 0
    next_cursor: int = 0
    eof: bool = False
    capture_complete: bool = False
    retention: Retention = Retention.CAPTURED
    failure: ExecutionFailure | None = None
    deadline_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class JobStop:
    """Permanent stop intent and separately established resource termination.

    None means acceptance is uncertain. Termination does not resolve the
    operation owner's independent helper, launch or keeper obligations.
    """

    reference: JobRef
    accepted: bool | None = False
    terminated: bool = False
    failure: ExecutionFailure | None = None
    deadline_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class JobDisposal:
    """Exact artifact disposal; None means delivery or publication is uncertain.

    Confirmation settles only disposal, not original launch or owner debt.
    """

    reference: JobRef
    disposed: bool | None = False
    failure: ExecutionFailure | None = None
    deadline_exceeded: bool = False
