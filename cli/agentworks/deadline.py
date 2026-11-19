"""When a local wait budget runs out, on the monotonic clock: shared by the waits that spend it."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Deadline:
    """One local wait budget; ``expires_at`` of ``None`` is unbounded.

    It bounds how long the caller waits, never what it waited on: running
    out does not stop a guest command or a provider call. Every wait that
    takes it gets what the ones before it left. ``seconds`` is the budget it
    was made with by :meth:`after`, for messages that name it.
    """

    expires_at: float | None
    seconds: float | None = None

    @classmethod
    def after(cls, seconds: float | None) -> Deadline:
        return cls(None if seconds is None else time.monotonic() + seconds, seconds)

    def remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.monotonic())

    @property
    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0
