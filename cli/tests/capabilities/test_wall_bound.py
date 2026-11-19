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
from agentworks.deadline import Deadline

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
    started, let_finish = threading.Event(), threading.Event()
    worker: list[threading.Thread] = []

    def _call() -> str:
        worker.append(threading.current_thread())
        started.set()
        let_finish.wait(_CAP)
        return "finished"

    class _Clock:
        """Reads 0 for the deadline and the first wait, then lets the call finish and reads past the bound.

        It counts on ``call_within``'s order of reads: the deadline, the first wait's remaining time, and
        then the check of whether the bound has run out, the read this clock finishes the call during.
        """

        def __init__(self) -> None:
            self.reads = 0

        def monotonic(self) -> float:
            self.reads += 1
            if self.reads < 3:
                return 0.0
            started.wait(_CAP)  # the worker is running the call, so it is there to join
            let_finish.set()
            worker[0].join(_CAP)  # the worker has returned and marked itself finished
            return 1.0

    monkeypatch.setattr(wall_bound, "time", _Clock())

    assert call_within(_call, 1.0) == "finished"


def test_a_call_with_nothing_left_of_its_deadline_is_not_dispatched() -> None:
    dispatched: list[bool] = []

    with pytest.raises(CallTimedOut):
        call_within(lambda: dispatched.append(True), Deadline.after(0.0))

    assert dispatched == []


def test_calls_sharing_a_deadline_each_get_what_the_ones_before_left(release: threading.Event) -> None:
    deadline = Deadline.after(0.4)
    call_within(lambda: release.wait(0.3), deadline)

    started = time.monotonic()
    with pytest.raises(CallTimedOut) as caught:
        call_within(lambda: release.wait(_CAP), deadline)
    elapsed = time.monotonic() - started

    assert elapsed < 0.3
    assert caught.value.seconds == 0.4


def test_a_bounded_client_bounds_each_method_and_refuses_other_attributes() -> None:
    """A nested group of methods read as an attribute would be called unbounded."""
    client = SimpleNamespace(group=SimpleNamespace(), where=lambda: threading.current_thread().name)
    bounded = BoundedClient(client, 5.0)

    assert bounded.where() == wall_bound.THREAD_NAME
    with pytest.raises(TypeError):
        _ = bounded.group
