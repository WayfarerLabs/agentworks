"""A bounded provider call ends at its bound on the wall clock, and Ctrl-C still ends it promptly."""

from __future__ import annotations

import signal
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform import wall_bound
from agentworks.capabilities.vm_platform.wall_bound import BoundedClient, CallTimedOut, call_within

if TYPE_CHECKING:
    from collections.abc import Iterator

# How long a blocked call waits for release before it returns anyway, so a wait that does not end at its bound
# fails the test rather than hanging it. A caller that returned well before the cap returned at the bound.
_CAP = 10.0


@pytest.fixture
def release() -> Iterator[threading.Event]:
    """What a blocked call waits on; set at teardown so no call outlives the test."""
    event = threading.Event()
    yield event
    event.set()


def test_a_call_that_outlives_its_bound_raises_at_the_bound(release: threading.Event) -> None:
    started = time.monotonic()
    with pytest.raises(CallTimedOut):
        call_within(lambda: release.wait(_CAP), 0.2)
    elapsed = time.monotonic() - started

    assert 0.2 <= elapsed < _CAP / 2


def test_what_the_call_raised_is_raised_to_the_caller_interrupts_included() -> None:
    """Fakes raise ``KeyboardInterrupt`` from inside a provider call to stand for Ctrl-C there."""

    def _interrupted() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        call_within(_interrupted, 5.0)


@pytest.mark.windows
def test_ctrl_c_ends_a_bounded_call_promptly(release: threading.Event) -> None:
    """Interrupts are not held for a bounded call, even when the signal reaches the call's own thread.

    The signal comes once the caller is surely waiting, since one that lands before is handled at once anyway.
    """

    def _ctrl_c_once_waiting() -> None:
        time.sleep(1.0)
        signal.raise_signal(signal.SIGINT)
        release.wait(_CAP)

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        call_within(_ctrl_c_once_waiting, 300.0)
    elapsed = time.monotonic() - started

    assert elapsed < _CAP / 2


def test_a_call_that_finishes_as_its_bound_runs_out_counts_as_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait can end unfinished just before the call does; the call's result still wins over the bound."""
    let_finish, returned = threading.Event(), threading.Event()

    def _call() -> str:
        let_finish.wait(_CAP)
        returned.set()
        return "finished"

    class _Clock:
        """Reads 0 until the bound is checked after a wait, then lets the call finish and reads past the bound."""

        def __init__(self) -> None:
            self.reads = 0

        def monotonic(self) -> float:
            self.reads += 1
            if self.reads < 3:  # the deadline, then the first wait's remaining time
                return 0.0
            let_finish.set()
            returned.wait(_CAP)
            time.sleep(0.1)  # the worker marks itself finished just after its call returns
            return 1.0

    monkeypatch.setattr(wall_bound, "time", _Clock())

    assert call_within(_call, 0.0) == "finished"


def test_a_bounded_client_runs_each_method_on_a_bounded_call_and_reads_other_attributes_through() -> None:
    client = SimpleNamespace(region="r1", where=lambda: threading.current_thread().name)
    bounded = BoundedClient(client, 5.0)

    assert bounded.where() == wall_bound.THREAD_NAME
    assert bounded.region == "r1"
