"""Concurrency contracts shared by read-only status observers."""

from __future__ import annotations

from concurrent.futures import Future

import pytest

from agentworks.errors import UserAbort
from agentworks.status_observation import cancelling_futures


def test_exception_cancels_every_queued_future_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    futures: list[Future[object]] = []
    shutdown_calls: list[tuple[bool, bool]] = []

    class _Executor:
        def __init__(self, *, max_workers: int) -> None:
            assert max_workers == 2

        def submit(self, _callable: object, *_args: object) -> Future[object]:
            future: Future[object] = Future()
            futures.append(future)
            return future

        def shutdown(self, *, wait: bool, cancel_futures: bool = False) -> None:
            shutdown_calls.append((wait, cancel_futures))
            if cancel_futures:
                for future in futures:
                    future.cancel()

    monkeypatch.setattr("agentworks.status_observation.ThreadPoolExecutor", _Executor)

    with pytest.raises(UserAbort), cancelling_futures({"one": lambda: None, "two": lambda: None}):
        raise UserAbort("interrupted")

    assert all(future.cancelled() for future in futures)
    assert shutdown_calls == [(False, True)]


def test_submission_exception_cancels_already_queued_work_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[Future[object]] = []
    shutdown_calls: list[tuple[bool, bool]] = []

    class _Executor:
        def __init__(self, *, max_workers: int) -> None:
            assert max_workers == 2

        def submit(self, _callable: object, *_args: object) -> Future[object]:
            if submitted:
                raise UserAbort("interrupted")
            future: Future[object] = Future()
            submitted.append(future)
            return future

        def shutdown(self, *, wait: bool, cancel_futures: bool = False) -> None:
            shutdown_calls.append((wait, cancel_futures))
            if cancel_futures:
                for future in submitted:
                    future.cancel()

    monkeypatch.setattr("agentworks.status_observation.ThreadPoolExecutor", _Executor)

    with pytest.raises(UserAbort), cancelling_futures({"one": lambda: None, "two": lambda: None}):
        pass

    assert submitted[0].cancelled()
    assert shutdown_calls == [(False, True)]
