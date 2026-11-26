"""Run a second operation while a first one is paused inside its work, as another agw invocation would.

A pair test drives the first operation through its real manager and pauses
it at a known guest command or launch, under the lock set it holds there.
At that point the second operation runs to completion on its own database
connection and in a fresh context, so it shares nothing with the first but
the lock directory, exactly as a second process would: every lock is its own
open file description, so the two refuse each other as two processes do,
and the first's held sets are invisible to the second's prompt checks.
Running the second inline at the pause, rather than on a thread, keeps the
interleaving deterministic.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.db import Database
from agentworks.native_locks import NativeLockBusyError, operation_label

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


@dataclass
class Outcome:
    """What the second operation did: returned, or raised ``error``."""

    error: BaseException | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None

    def refusal(self) -> NativeLockBusyError:
        """The lock refusal it raised, failing the test if it raised anything else or nothing."""
        assert isinstance(self.error, NativeLockBusyError), f"expected a lock refusal, got {self.error!r}"
        return self.error


def another_invocation(database: Path, label: tuple[str, ...], operation: Callable[[Database], object]) -> Outcome:
    """Run ``operation`` as a separate invocation labelled ``label``, and report how it ended."""

    def run() -> Outcome:
        operation_label.set(label)
        db = Database(database)
        try:
            operation(db)
        except Exception as error:
            return Outcome(error)
        finally:
            db.close()
        return Outcome()

    return contextvars.Context().run(run)


@contextmanager
def labelled(label: tuple[str, ...]) -> Iterator[None]:
    """Run the first operation under ``label``, the command path the CLI would record for it."""
    token = operation_label.set(label)
    try:
        yield
    finally:
        operation_label.reset(token)


class Pause:
    """The point the first operation pauses at: the first time it is reached, the second operation runs."""

    def __init__(self, database: Path, label: tuple[str, ...], operation: Callable[[Database], object]) -> None:
        self._database = database
        self._label = label
        self._operation = operation
        self.outcome: Outcome | None = None

    def reached(self) -> None:
        if self.outcome is None:
            self.outcome = Outcome()  # Mark first, so the second operation reaching this point does not re-enter.
            self.outcome = another_invocation(self._database, self._label, self._operation)

    def result(self) -> Outcome:
        assert self.outcome is not None, "the first operation never reached its pause"
        return self.outcome
