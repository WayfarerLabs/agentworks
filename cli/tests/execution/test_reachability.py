"""``reachability.wait``: what it polls, what it raises at once, and how it confirms an answer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.execution import reachability
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete, TrustMismatch, TrustMissing, VMNotReady
from agentworks.execution.results import (
    Completed,
    NotDelivered,
    NotDeliveredReason,
    Result,
    Unknown,
    UnknownReason,
)
from tests.vm_access_support import Admin, RecordingVMAccess

if TYPE_CHECKING:
    from tests.vm_access_support import Call

CONNECTION = Result(Unknown(UnknownReason.CONNECTION))
ANSWER = Result(Completed(0))


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr("agentworks.execution.reachability.time.sleep", slept.append)
    return slept


def access(*outcomes: Result | BaseException) -> RecordingVMAccess:
    calls: list[Call] = []
    recording = RecordingVMAccess(calls, Admin())
    recording.run_outcomes = list(outcomes)
    return recording


def probes(recording: RecordingVMAccess) -> int:
    return len(recording.calls)


def test_an_answer_on_the_first_attempt_returns_at_once(sleeps: list[float]) -> None:
    recording = access(ANSWER)

    assert reachability.wait(recording, wording=reachability.Wording.VERIFY) is True
    assert probes(recording) == 1
    assert sleeps == []
    assert recording.calls[0].request == reachability.PROBE


def test_an_answer_after_a_failure_is_confirmed_by_a_second_probe(sleeps: list[float]) -> None:
    recording = access(CONNECTION, ANSWER, ANSWER)

    assert reachability.wait(recording, wording=reachability.Wording.RECONNECT) is True
    assert probes(recording) == 3
    assert sleeps == [reachability.PAUSE_SECONDS, reachability.CONFIRM_PAUSE_SECONDS]


def test_a_failed_confirmation_keeps_polling(sleeps: list[float]) -> None:
    recording = access(CONNECTION, ANSWER, CONNECTION, ANSWER, ANSWER)

    assert reachability.wait(recording, wording=reachability.Wording.RECONNECT) is True
    assert probes(recording) == 5


@pytest.mark.parametrize("reason", [UnknownReason.CONNECTION, UnknownReason.TRUNCATED, UnknownReason.DEADLINE])
def test_exhaustion_returns_false_after_every_attempt_and_no_trailing_pause(
    reason: UnknownReason, sleeps: list[float]
) -> None:
    recording = access(*[Result(Unknown(reason))] * reachability.ATTEMPTS)

    assert reachability.wait(recording, wording=reachability.Wording.VERIFY) is False
    assert probes(recording) == reachability.ATTEMPTS
    assert sleeps == [reachability.PAUSE_SECONDS] * (reachability.ATTEMPTS - 1)


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (Result(Unknown(UnknownReason.HOST_KEY)), TrustMismatch),
        (Result(Unknown(UnknownReason.AUTH_FAILED)), ExecutionIncomplete),
        (Result(Unknown(UnknownReason.OUTPUT_LIMIT)), ExecutionIncomplete),
        (Result(NotDelivered(NotDeliveredReason.LAUNCHER_FAILED)), ExecutionIncomplete),
        (Result(NotDelivered(NotDeliveredReason.PYTHON_MISSING)), VMNotReady),
        (CommandFailed("box", 1, Result(Completed(1)), streams_logged=True), CommandFailed),
        (TrustMissing("box"), TrustMissing),
    ],
    ids=["host-key", "auth-failed", "output-limit", "launcher-failed", "python-missing", "true-failed", "no-record"],
)
def test_a_failure_waiting_cannot_fix_raises_on_the_first_attempt(
    outcome: Result | BaseException, error: type[BaseException], sleeps: list[float]
) -> None:
    recording = access(outcome, *[ANSWER] * reachability.ATTEMPTS)

    with pytest.raises(error):
        reachability.wait(recording, wording=reachability.Wording.VERIFY)
    assert probes(recording) == 1
    assert sleeps == []
