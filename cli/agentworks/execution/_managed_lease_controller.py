"""Remembered expiry and irreversible stop latch for the existing controller."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._managed_lease_store import read_lease
from ._managed_lease_wire import LeaseError, boottime_ns, checked_lease

if TYPE_CHECKING:
    from ._managed_job_store import ManagedJobStore


@dataclass(slots=True)
class LeaseControl:
    launch: bytes
    expires_ns: int
    closed: bool = False

    def stop_due(self, store: ManagedJobStore) -> bool:
        if self.closed:
            return True
        try:
            now = boottime_ns()
        except (LeaseError, OSError):
            self.closed = True
            return True
        if now >= self.expires_ns:
            self.closed = True
            return True
        try:
            candidate = read_lease(store, self.launch)
            if candidate is not None:
                checked_lease(candidate, self.launch, now)
                if candidate.expires_ns > self.expires_ns:
                    self.expires_ns = candidate.expires_ns
        except (LeaseError, ValueError, OSError):
            # Unavailable control never extends the last accepted expiry.
            pass
        return False
