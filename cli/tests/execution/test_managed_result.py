"""One-shot managed result reduction against persisted runs and real ownership."""

from __future__ import annotations

from agentworks.execution._delivery_custody import LocalDeliveryCustody
import hashlib
import time
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution import _managed_result as result_module
from agentworks.execution._managed_job_store import FactName, Stream
from agentworks.execution._managed_observation_protocol import ManagedOperation, ManagedResultControl
from agentworks.execution._managed_observe_access import read_bound_managed_output
from agentworks.execution._managed_result import (
    ManagedResultControlFact,
    collect_bound_managed_result,
    wait_bound_managed_result,
)
from agentworks.execution._managed_runs import (
    ManagedLaunchObservation,
    ManagedOutputMode,
    ManagedOutputPolicy,
    ManagedRunReceipt,
)
from agentworks.execution.carrier import Deadline, Dispatch, Retention
from agentworks.execution.result import ApplicationState, ExecutionFailure, ExitCode, Signal

from .test_managed_observation import ScriptedCarrier, _fact, _records
from .test_managed_observe_access import RUN, _options, _reserved


def _confirmed(tmp_path: Path, policy: ManagedOutputPolicy | None = None):  # type: ignore[no-untyped-def]
    database, repository, owner = _reserved(tmp_path, policy)
    reserved = repository.inspect(RUN)
    assert reserved is not None
    possible = repository.mark_possible_dispatch(reserved)
    repository.reconcile(
        possible,
        ManagedLaunchObservation(
            Dispatch.SENT,
            ManagedRunReceipt(possible.identity, possible.identity.unit_name, possible.spec),
        ),
    )
    return database, repository, owner


def _reply(
    request,
    *,
    wait: int | None = 0,
    signal: int | None = None,
    boundary: bool = True,
    ends: tuple[Stream, ...] = (Stream.STDOUT, Stream.STDERR),
    disposition: str = "complete-capture",
) -> bytes:  # type: ignore[no-untyped-def]
    launch = request.expected_launch
    if request.operation is ManagedOperation.READ_OUTPUT:
        assert request.stream is not None
        end_name = FactName.STDOUT_END if request.stream is Stream.STDOUT else FactName.STDERR_END
        content = b"out" if request.stream is Stream.STDOUT else b"err"
        end = _fact(end_name, launch, disposition=disposition, content=content if "capture" in disposition else b"")
        return _records(
            request.nonce,
            ManagedResultControl((FactName.LAUNCH, end_name)),
            (launch, end),
            content if "capture" in disposition else b"",
        )
    names = [FactName.LAUNCH]
    facts = [launch]
    if wait is not None or signal is not None:
        names.append(FactName.WAIT)
        facts.append(
            wire.encode_fact(
                {
                    "version": 1,
                    "run_id": RUN.run_id,
                    "unit": RUN.unit_name,
                    "receipt_sha256": hashlib.sha256(launch).hexdigest(),
                    "kind": "wait",
                    "exit_code": wait,
                    "signal": signal,
                }
            )
        )
    for stream in ends:
        name = FactName.STDOUT_END if stream is Stream.STDOUT else FactName.STDERR_END
        content = (b"out" if stream is Stream.STDOUT else b"err") if "capture" in disposition else b""
        names.append(name)
        facts.append(_fact(name, launch, disposition=disposition, content=content))
    if boundary:
        names.append(FactName.BOUNDARY_EMPTY)
        facts.append(_fact(FactName.BOUNDARY_EMPTY, launch))
    return _records(request.nonce, ManagedResultControl(tuple(names)), tuple(facts))


@pytest.mark.parametrize(
    ("policy", "disposition", "retention"),
    [
        (ManagedOutputPolicy(ManagedOutputMode.CAPTURE, 4096), "complete-capture", Retention.CAPTURED),
        (ManagedOutputPolicy(ManagedOutputMode.DISCARD), "discarded", Retention.DISCARDED),
        (ManagedOutputPolicy(ManagedOutputMode.SENSITIVITY_SUPPRESSED), "sensitivity-suppressed", Retention.SUPPRESSED),
    ],
)
def test_exact_zero_and_settled_streams_succeed(
    tmp_path: Path, policy: ManagedOutputPolicy, disposition: str, retention: Retention
) -> None:
    database, repository, owner = _confirmed(tmp_path, policy)
    carrier = ScriptedCarrier(lambda request: _reply(request, disposition=disposition))
    try:
        outcome = collect_bound_managed_result(repository, RUN, **_options(owner, carrier))  # type: ignore[arg-type]
        result = outcome.result
        assert result.ok
        assert result.dispatch is Dispatch.UNKNOWN
        assert result.application_state is ApplicationState.COMPLETED
        assert result.status == ExitCode(0)
        assert result.stdout.retention is result.stderr.retention is retention
        assert result.stdout.data == (b"out" if retention is Retention.CAPTURED else b"")
        assert result.stderr.data == (b"err" if retention is Retention.CAPTURED else b"")
        assert result.owned_cleanup_confirmed
        assert not outcome.requires_owner_retention
        assert carrier.calls == 3
    finally:
        database.close()


@pytest.mark.parametrize(
    ("wait", "signal", "boundary", "ends", "disposition", "failure", "calls"),
    [
        (7, None, True, (Stream.STDOUT, Stream.STDERR), "complete-capture", None, 3),
        (None, 9, True, (Stream.STDOUT, Stream.STDERR), "complete-capture", None, 3),
        (0, None, False, (Stream.STDOUT, Stream.STDERR), "complete-capture", ExecutionFailure.OBSERVATION, 3),
        (0, None, True, (Stream.STDOUT,), "complete-capture", ExecutionFailure.OUTPUT, 2),
        (0, None, True, (Stream.STDOUT, Stream.STDERR), "truncated-capture", ExecutionFailure.OUTPUT_LIMIT, 3),
        (None, None, True, (Stream.STDOUT, Stream.STDERR), "complete-capture", ExecutionFailure.OBSERVATION, 3),
    ],
)
def test_independent_wait_stream_and_boundary_facts(
    tmp_path: Path,
    wait: int | None,
    signal: int | None,
    boundary: bool,
    ends: tuple[Stream, ...],
    disposition: str,
    failure: ExecutionFailure | None,
    calls: int,
) -> None:
    database, repository, owner = _confirmed(tmp_path)
    carrier = ScriptedCarrier(
        lambda request: _reply(request, wait=wait, signal=signal, boundary=boundary, ends=ends, disposition=disposition)
    )
    try:
        outcome = collect_bound_managed_result(repository, RUN, **_options(owner, carrier))  # type: ignore[arg-type]
        result = outcome.result
        assert not result.ok
        assert result.failure is failure
        assert result.owned_cleanup_confirmed is boundary
        assert result.application_state is (
            ApplicationState.UNKNOWN if wait is None and signal is None else ApplicationState.COMPLETED
        )
        if signal is not None:
            assert result.status == Signal(signal)
        elif wait is not None:
            assert result.status == ExitCode(wait)
        if disposition == "truncated-capture":
            assert result.stdout.data == b"out"
            assert not result.stdout.complete
        assert carrier.calls == calls
    finally:
        database.close()


def test_ambiguous_observation_retains_owner_and_stops(tmp_path: Path) -> None:
    database, repository, owner = _confirmed(tmp_path)
    carrier = ScriptedCarrier(lambda request: _reply(request), dispatch=Dispatch.UNKNOWN, code=None)
    try:
        outcome = collect_bound_managed_result(repository, RUN, **_options(owner, carrier))  # type: ignore[arg-type]
        assert carrier.calls == 1
        assert outcome.requires_owner_retention
        assert outcome.result.application_state is ApplicationState.UNKNOWN
        assert outcome.result.failure is ExecutionFailure.OBSERVATION
        assert not outcome.result.ok
    finally:
        database.close()


@pytest.mark.parametrize("possible", [False, True])
def test_requires_reconciled_launch_before_carrier(tmp_path: Path, possible: bool) -> None:
    database, repository, owner = _reserved(tmp_path)
    reserved = repository.inspect(RUN)
    assert reserved is not None
    if possible:
        repository.mark_possible_dispatch(reserved)
    carrier = ScriptedCarrier(lambda request: _reply(request))
    try:
        with pytest.raises(ValidationError):
            collect_bound_managed_result(repository, RUN, **_options(owner, carrier))  # type: ignore[arg-type]
        assert carrier.calls == 0
    finally:
        database.close()


def test_second_stream_interruption_carries_all_attempt_custody(tmp_path: Path) -> None:
    database, repository, owner = _confirmed(tmp_path)
    interruption = KeyboardInterrupt("second stream")

    class InterruptedCarrier(ScriptedCarrier):
        def execute(self, invocation, *, io, deadline, custody: LocalDeliveryCustody | None = None):  # type: ignore[no-untyped-def]
            if self.calls == 2:
                self.calls += 1
                raise interruption
            return super().execute(invocation, io=io, deadline=deadline, custody=custody)

    carrier = InterruptedCarrier(lambda request: _reply(request))
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            collect_bound_managed_result(repository, RUN, **_options(owner, carrier))  # type: ignore[arg-type]
        assert raised.value is interruption
        assert isinstance(interruption.__cause__, ManagedResultControlFact)
        assert len(interruption.__cause__.attempts) == 3
        assert interruption.__cause__.attempts[2].requires_owner_retention
        assert carrier.calls == 3
    finally:
        database.close()


def test_first_observation_interruption_uses_collector_custody_fact(tmp_path: Path) -> None:
    database, repository, owner = _confirmed(tmp_path)
    interruption = KeyboardInterrupt("observe interrupted")

    class InterruptedCarrier(ScriptedCarrier):
        def execute(self, invocation, *, io, deadline, custody: LocalDeliveryCustody | None = None):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise interruption

    carrier = InterruptedCarrier(lambda request: _reply(request))
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            collect_bound_managed_result(repository, RUN, **_options(owner, carrier))  # type: ignore[arg-type]
        assert raised.value is interruption
        assert isinstance(interruption.__cause__, ManagedResultControlFact)
        assert len(interruption.__cause__.attempts) == 1
        assert interruption.__cause__.attempts[0].requires_owner_retention
        assert carrier.calls == 1
    finally:
        database.close()


def test_deadline_crossing_before_output_admission_returns_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, owner = _confirmed(tmp_path)
    carrier = ScriptedCarrier(lambda request: _reply(request))
    deadline = Deadline.after(10)
    real_read = read_bound_managed_output

    def expire_before_read(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        object.__setattr__(deadline, "expires_at", 0.0)
        return real_read(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(result_module, "read_bound_managed_output", expire_before_read)
    try:
        outcome = collect_bound_managed_result(
            repository,
            RUN,
            **_options(owner, carrier, deadline=deadline),  # type: ignore[arg-type]
        )
        assert outcome.result.application_state is ApplicationState.COMPLETED
        assert outcome.result.status == ExitCode(0)
        assert outcome.result.failure is ExecutionFailure.DEADLINE
        assert outcome.result.deadline_exceeded
        assert not outcome.result.ok
        assert not outcome.requires_owner_retention
        assert len(outcome.attempts) == 1
        assert carrier.calls == 1
    finally:
        database.close()


def test_unrelated_admission_refusal_is_not_rewritten_as_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, owner = _confirmed(tmp_path)
    carrier = ScriptedCarrier(lambda request: _reply(request))
    deadline = Deadline.after(10)
    real_inspect = repository.inspect
    inspected = 0

    def inspect(identity):  # type: ignore[no-untyped-def]
        nonlocal inspected
        inspected += 1
        if inspected == 3:
            object.__setattr__(deadline, "expires_at", 0.0)
            return None
        return real_inspect(identity)

    monkeypatch.setattr(repository, "inspect", inspect)
    try:
        with pytest.raises(ValidationError) as raised:
            collect_bound_managed_result(
                repository,
                RUN,
                **_options(owner, carrier, deadline=deadline),  # type: ignore[arg-type]
            )
        assert isinstance(raised.value.__cause__, ManagedResultControlFact)
        assert len(raised.value.__cause__.attempts) == 1
        assert carrier.calls == 1
    finally:
        database.close()


def test_wait_polls_only_clean_pending_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, repository, owner = _confirmed(tmp_path)
    observations = 0

    def reply(request):  # type: ignore[no-untyped-def]
        nonlocal observations
        if request.operation is ManagedOperation.OBSERVE:
            observations += 1
            if observations == 1:
                return _reply(request, wait=None, boundary=False, ends=())
        return _reply(request)

    carrier = ScriptedCarrier(reply)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    try:
        outcome = wait_bound_managed_result(repository, RUN, **_options(owner, carrier))  # type: ignore[arg-type]
        assert observations == 2
        assert carrier.calls == 4
        assert outcome.result.ok
        assert not outcome.awaiting_facts
        assert not outcome.requires_owner_retention
    finally:
        database.close()


def test_wait_expiry_returns_partial_evidence_without_repoll(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, repository, owner = _confirmed(tmp_path)
    carrier = ScriptedCarrier(lambda request: _reply(request, wait=None, boundary=False, ends=()))
    deadline = Deadline.after(10)

    def expire(seconds: float) -> None:
        object.__setattr__(deadline, "expires_at", 0.0)

    monkeypatch.setattr(time, "sleep", expire)
    try:
        outcome = wait_bound_managed_result(
            repository,
            RUN,
            **_options(owner, carrier, deadline=deadline),  # type: ignore[arg-type]
        )
        assert carrier.calls == 1
        assert outcome.result.failure is ExecutionFailure.DEADLINE
        assert outcome.result.deadline_exceeded
        assert outcome.result.application_state is ApplicationState.UNKNOWN
        assert not outcome.requires_owner_retention
        assert not outcome.awaiting_facts
    finally:
        database.close()


def test_wait_expiry_at_next_poll_admission_keeps_last_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, repository, owner = _confirmed(tmp_path)
    carrier = ScriptedCarrier(lambda request: _reply(request, wait=None, boundary=False, ends=()))
    deadline = Deadline.after(10)
    real_collect = result_module.collect_bound_managed_result
    collections = 0

    def collect(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal collections
        collections += 1
        if collections == 2:
            object.__setattr__(deadline, "expires_at", 0.0)
        return real_collect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(result_module, "collect_bound_managed_result", collect)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    try:
        outcome = wait_bound_managed_result(
            repository,
            RUN,
            **_options(owner, carrier, deadline=deadline),  # type: ignore[arg-type]
        )
        assert collections == 2
        assert carrier.calls == 1
        assert outcome.result.failure is ExecutionFailure.DEADLINE
        assert outcome.result.deadline_exceeded
        assert outcome.result.application_state is ApplicationState.UNKNOWN
        assert len(outcome.attempts) == 1
    finally:
        database.close()


def test_wait_nonzero_main_exit_still_observes_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database, repository, owner = _confirmed(tmp_path)
    observations = 0

    def reply(request):  # type: ignore[no-untyped-def]
        nonlocal observations
        if request.operation is ManagedOperation.OBSERVE:
            observations += 1
            if observations == 1:
                return _reply(request, wait=7, boundary=False, ends=())
        return _reply(request, wait=7)

    carrier = ScriptedCarrier(reply)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    try:
        outcome = wait_bound_managed_result(repository, RUN, **_options(owner, carrier))  # type: ignore[arg-type]
        assert observations == 2
        assert outcome.result.status == ExitCode(7)
        assert outcome.result.owned_cleanup_confirmed
        assert outcome.result.failure is None
        assert not outcome.result.ok
    finally:
        database.close()


def test_wait_does_not_retry_uncertain_observation(tmp_path: Path) -> None:
    database, repository, owner = _confirmed(tmp_path)
    carrier = ScriptedCarrier(lambda request: _reply(request), dispatch=Dispatch.UNKNOWN, code=None)
    try:
        outcome = wait_bound_managed_result(repository, RUN, **_options(owner, carrier))  # type: ignore[arg-type]
        assert carrier.calls == 1
        assert outcome.requires_owner_retention
        assert not outcome.awaiting_facts
    finally:
        database.close()


@pytest.mark.parametrize(
    ("wait", "disposition", "failure"),
    [
        (7, "complete-capture", None),
        (0, "truncated-capture", ExecutionFailure.OUTPUT_LIMIT),
    ],
)
def test_wait_does_not_retry_terminal_outcomes(
    tmp_path: Path, wait: int, disposition: str, failure: ExecutionFailure | None
) -> None:
    database, repository, owner = _confirmed(tmp_path)
    carrier = ScriptedCarrier(lambda request: _reply(request, wait=wait, disposition=disposition))
    try:
        outcome = wait_bound_managed_result(repository, RUN, **_options(owner, carrier))  # type: ignore[arg-type]
        assert carrier.calls == 3
        assert outcome.result.status == ExitCode(wait)
        assert outcome.result.failure is failure
        assert not outcome.awaiting_facts
    finally:
        database.close()
