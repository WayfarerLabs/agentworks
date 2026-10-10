"""Inert startup and retained completion use no native process or SSH resources."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Thread

import pytest

from tests.execution.carriers.ssh import fixture_worker as guard


@pytest.mark.parametrize("started", [False, True])
def test_interrupted_start_cancels_a_started_or_late_native_tail(
    monkeypatch: pytest.MonkeyPatch, started: bool
) -> None:
    effects: list[object] = []
    interruption = KeyboardInterrupt()
    threads: list[tuple[Thread, Callable[[], None]]] = []

    def make_thread(*, target: Callable[[], None]) -> Thread:
        thread = Thread(target=target)
        start = thread.start

        def interrupt() -> None:
            if started:
                start()
            raise interruption

        monkeypatch.setattr(thread, "start", interrupt)
        threads.append((thread, start))
        return thread

    monkeypatch.setattr(guard, "Thread", make_thread)
    with (
        pytest.raises(BaseExceptionGroup) as caught,
        guard.FixtureWorker(lambda stop: effects.append(object())) as worker,
    ):
        worker.start()
        pytest.fail("Failed start admitted caller work")
    assert caught.value.exceptions == (interruption,)
    thread, start = threads[0]
    if not started:
        start()
    thread.join(timeout=2)
    assert not thread.is_alive() and effects == []


def test_caller_control_waits_constructor_and_cleanup_before_outer_resources_close() -> None:
    entered, release, stopped = Event(), Event(), Event()
    primary = KeyboardInterrupt()
    order: list[str] = []
    failures: list[BaseException] = []

    def lifetime(stop: Event) -> None:
        entered.set()
        assert release.wait(timeout=5)
        order.append("constructor-returned")
        assert stop.wait(timeout=5)
        order.append("process-cleaned")

    def caller() -> None:
        try:
            with guard.FixtureWorker(lifetime) as worker:
                worker.start()
                assert entered.wait(timeout=2)
                stopped.set()
                raise primary
        except BaseException as error:
            failures.append(error)
        finally:
            order.append("outer-closed")

    supervisor = Thread(target=caller)
    supervisor.start()
    try:
        assert stopped.wait(timeout=2)
        assert order == []
    finally:
        release.set()
        supervisor.join(timeout=3)
    assert not supervisor.is_alive()
    assert order == ["constructor-returned", "process-cleaned", "outer-closed"]
    assert len(failures) == 1 and isinstance(failures[0], BaseExceptionGroup)
    assert failures[0].exceptions == (primary,)


def test_constructor_failure_publishes_before_outer_cleanup() -> None:
    primary = OSError("synthetic constructor failure")
    order: list[str] = []

    def construct(stop: Event) -> None:
        try:
            raise primary
        finally:
            order.append("constructor-finished")

    try:
        with pytest.raises(BaseExceptionGroup) as caught:
            guard.FixtureWorker(construct).run()
        assert caught.value.exceptions == (primary,)
    finally:
        order.append("outer-closed")
    assert order == ["constructor-finished", "outer-closed"]


def test_cleanup_failure_and_caller_control_are_both_retained() -> None:
    primary, cleanup = KeyboardInterrupt(), OSError("synthetic cleanup failure")
    ready = Event()

    def lifetime(stop: Event) -> None:
        ready.set()
        assert stop.wait(timeout=5)
        raise cleanup

    with pytest.raises(BaseExceptionGroup) as caught, guard.FixtureWorker(lifetime) as worker:
        worker.start()
        assert ready.wait(timeout=2)
        raise primary
    assert caught.value.exceptions == (primary, cleanup)


def test_repeated_completion_controls_cannot_release_outer_resources_early() -> None:
    first, second = KeyboardInterrupt(), SystemExit(17)
    waiting, release, ready = Event(), Event(), Event()
    order: list[str] = []
    failures: list[BaseException] = []

    class InterruptedCompletion(Event):
        def __init__(self) -> None:
            super().__init__()
            self.controls = iter((first, second))

        def wait(self, timeout: float | None = None) -> bool:
            control = next(self.controls, None)
            if control is not None:
                raise control
            waiting.set()
            return super().wait(timeout)

    def lifetime(stop: Event) -> None:
        ready.set()
        assert stop.wait(timeout=5)
        assert release.wait(timeout=5)
        order.append("process-cleaned")

    attempt = guard.FixtureWorker(lifetime)
    attempt._done = InterruptedCompletion()

    def caller() -> None:
        try:
            with attempt:
                attempt.start()
                assert ready.wait(timeout=2)
        except BaseException as error:
            failures.append(error)
        finally:
            order.append("outer-closed")

    supervisor = Thread(target=caller)
    supervisor.start()
    try:
        assert waiting.wait(timeout=2)
        assert order == []
    finally:
        release.set()
        supervisor.join(timeout=3)
    assert not supervisor.is_alive()
    assert order == ["process-cleaned", "outer-closed"]
    assert len(failures) == 1 and isinstance(failures[0], BaseExceptionGroup)
    assert failures[0].exceptions == (first, second)


def test_passive_entry_control_cannot_admit_effects() -> None:
    effects: list[object] = []
    primary = KeyboardInterrupt()
    attempt = guard.FixtureWorker(lambda stop: effects.append(object()))
    with pytest.raises(BaseExceptionGroup) as caught, attempt:
        assert effects == []
        raise primary
    assert caught.value.exceptions == (primary,)
    # Even an unexpectedly late tail stays inert after passive-entry cancellation.
    attempt._worker.start()
    attempt._worker.join(timeout=2)
    assert not attempt._worker.is_alive() and effects == []


def test_control_after_explicit_admission_waits_cleanup_before_outer_close(monkeypatch: pytest.MonkeyPatch) -> None:
    admitted_control, release = Event(), Event()
    primary = KeyboardInterrupt()
    order: list[str] = []
    failures: list[BaseException] = []

    def lifetime(stop: Event) -> None:
        assert release.wait(timeout=5)
        order.append("constructor-returned")
        assert stop.wait(timeout=5)
        order.append("process-cleaned")

    attempt = guard.FixtureWorker(lifetime)
    start = attempt.start

    def interrupt_return() -> None:
        start()
        admitted_control.set()
        raise primary

    monkeypatch.setattr(attempt, "start", interrupt_return)

    def caller() -> None:
        try:
            with attempt:
                attempt.start()
        except BaseException as error:
            failures.append(error)
        finally:
            order.append("outer-closed")

    supervisor = Thread(target=caller)
    supervisor.start()
    try:
        assert admitted_control.wait(timeout=2)
        assert order == []
    finally:
        release.set()
        supervisor.join(timeout=3)
    assert not supervisor.is_alive()
    assert order == ["constructor-returned", "process-cleaned", "outer-closed"]
    assert len(failures) == 1 and isinstance(failures[0], BaseExceptionGroup)
    assert failures[0].exceptions == (primary,)
