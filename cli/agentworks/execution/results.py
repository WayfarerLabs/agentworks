"""What came back from a request.

A completion says how far the request got, never more than the evidence shows:
``Completed`` carries the guest's own status, ``NotDelivered`` means the payload
never started, and ``Unknown`` means it may have run. Reasons are enums, never
stream text, because with no start marker a stream may quote request bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.execution.errors import (
    ExecutionIncomplete,
    RequestRefused,
    TrustMismatch,
    VMNotReady,
)

if TYPE_CHECKING:
    from pathlib import Path


class NotDeliveredReason(StrEnum):
    PYTHON_MISSING = "python_missing"
    STDLIB_MISSING = "stdlib_missing"
    LAUNCHER_FAILED = "launcher_failed"
    REFUSED = "refused"


class UnknownReason(StrEnum):
    CONNECTION = "connection"
    HOST_KEY = "host_key"
    TRUNCATED = "truncated"
    DEADLINE = "deadline"
    OUTPUT_LIMIT = "output_limit"


# What the guest's ``refused`` trailer may name. The text arrives from the
# guest, so anything else is a protocol violation, not a refusal to repeat.
REFUSAL_CODE = re.compile(r"cwd|handover|exec [A-Z][A-Z0-9]*")


@dataclass(frozen=True)
class Completed:
    """The payload ran to its end; ``status`` is its exit status, or 128 plus the signal."""

    status: int


@dataclass(frozen=True)
class NotDelivered:
    """The payload never started. ``code`` is the guest's refusal for ``REFUSED``."""

    reason: NotDeliveredReason
    code: str | None = None


@dataclass(frozen=True)
class Unknown:
    """The payload may have run."""

    reason: UnknownReason


Completion = Completed | NotDelivered | Unknown


@dataclass(frozen=True)
class Result:
    """One request's outcome.

    ``stdout`` and ``stderr`` are the payload's bytes, with no newline
    conversion. ``client_log_tail`` is the last lines of ssh's own diagnostics,
    which hold no payload output.
    """

    completion: Completion
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)
    client_log_tail: str = ""

    def require_completed(self, vm_name: str, *, identity_file: Path | None = None) -> int:
        """The status of a :class:`Completed`; otherwise raise the typed error for the outcome.

        ``identity_file`` is passed through to :class:`ExecutionIncomplete` for its key hint.
        """
        completion = self.completion
        if isinstance(completion, Completed):
            return completion.status
        if isinstance(completion, NotDelivered):
            if completion.reason in (NotDeliveredReason.PYTHON_MISSING, NotDeliveredReason.STDLIB_MISSING):
                raise VMNotReady(vm_name, completion.reason)
            if completion.reason is NotDeliveredReason.REFUSED:
                raise RequestRefused(vm_name, completion.code or "", self)
        elif completion.reason is UnknownReason.HOST_KEY:
            raise TrustMismatch(vm_name, self.client_log_tail)
        raise ExecutionIncomplete(vm_name, self, identity_file=identity_file)
