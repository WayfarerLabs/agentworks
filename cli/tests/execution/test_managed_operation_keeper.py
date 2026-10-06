"""Concrete keeper fencing and local custody with real framed exchanges."""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _managed_operation_keeper as keeper_module
from agentworks.execution._managed_job_protocol import encode_managed_job_fact
from agentworks.execution._managed_lease_protocol import LeaseRequest
from agentworks.execution._managed_operation_keeper import ManagedOperationKeeper
from agentworks.execution._managed_runs import (
    ManagedLaunchState,
    ManagedOutputMode,
    ManagedOutputPolicy,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunRecord,
)
from agentworks.execution._managed_start_exchange import (
    ManagedStartAttempt,
    ManagedStartCandidate,
    ManagedStartObservation,
    ManagedStartState,
)
from agentworks.execution._managed_start_operation import ManagedStartOutcome
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteObservation, RuntimePrerequisiteState
from agentworks.execution.carrier import Deadline, Dispatch, ExitStatus
from agentworks.operations import OperationOwner

from .test_managed_lease_exchange import PLAN, RUNTIME, ScriptedCarrier, _success
from .test_managed_start_operation import GUEST, RUN, _spec

# Real SQLite fencing and host-thread custody are also exercised on Windows.
pytestmark = pytest.mark.windows


@pytest.fixture
def bound(tmp_path: Path) -> Generator[tuple[Database, OperationOwner, ManagedRunReceipt]]:
    database = Database(tmp_path / "state.db")
    try:
        owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "keeper"
        )
        spec = replace(
            _spec(),
            lifetime=ManagedRunLifetime.OPERATION,
            owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, owner.ownership.operation_id),
        )
        yield database, owner, ManagedRunReceipt(RUN, RUN.unit_name, spec)
    finally:
        database.close()


def make_keeper(owner: OperationOwner, receipt: ManagedRunReceipt, carrier: ScriptedCarrier) -> ManagedOperationKeeper:
    return ManagedOperationKeeper(
        receipt,
        carrier,
        owner=owner,
        obligation_id="b" * 32,
        target=receipt.spec.target,
        guest=GUEST,
        root_plan=PLAN,
        runtime_selection=RUNTIME,
    )


def clean_start(receipt: ManagedRunReceipt) -> ManagedStartOutcome:
    record = ManagedRunRecord(
        receipt.identity,
        receipt.spec,
        ManagedOutputPolicy(ManagedOutputMode.CAPTURE, 4096),
        ManagedLaunchState.RECEIPT_CONFIRMED,
        "",
        "",
        "",
        "",
    )
    candidate = ManagedStartCandidate(
        Dispatch.SENT,
        ExitStatus(0),
        23,
        None,
        RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, "/usr/bin/python3"),
        ManagedStartObservation(ManagedStartState.ACKNOWLEDGED, 0, None, encode_managed_job_fact(receipt)),
    )
    return ManagedStartOutcome(ManagedStartAttempt(record, candidate), ManagedLaunchState.RECEIPT_CONFIRMED)


def test_passive_registration_sample_and_repeated_fences_during_real_borrow(bound) -> None:
    database, owner, receipt = bound
    carrier = ScriptedCarrier()
    keeper = make_keeper(owner, receipt, carrier)
    assert not keeper.registration_started and carrier.calls == 0
    assert owner.list_lifecycle_obligations() == ()
    try:
        keeper.admit()
        obligation = keeper.obligation
        assert obligation is not None and obligation.state is LifecycleObligationState.POSSIBLE_EFFECT
        borrow = owner.borrow()
        attempt = borrow.begin_attempt()
        connection = database._operation_connection_for_repository()
        changes = connection.total_changes
        initial = keeper.sample_initial(Deadline.after(1))
        assert initial.lease is not None and initial.lease.expires_ns - initial.lease.sampled_ns == 60_000_000_000
        assert connection.total_changes == changes
        with pytest.raises(StateError):
            owner.borrow()
        attempt.settle()
        borrow.close()
        with pytest.raises(StateError):
            keeper.sample_initial(Deadline.after(1))
        assert carrier.calls == 1
    finally:
        assert keeper.drain(Deadline.after(1)).drained


@pytest.mark.parametrize("fault", ["owner", "target", "boot", "root", "runtime"])
def test_initial_binding_refuses_before_registration(bound, fault: str) -> None:
    _, owner, receipt = bound
    if fault == "owner":
        receipt = replace(
            receipt, spec=replace(receipt.spec, owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "f" * 32))
        )
    target = replace(receipt.spec.target, name="other") if fault == "target" else receipt.spec.target
    guest = replace(GUEST, init_start_ticks=GUEST.init_start_ticks + 1) if fault == "boot" else GUEST
    plan = replace(PLAN, expected=replace(PLAN.expected, euid=1)) if fault == "root" else PLAN
    from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS

    runtime = RuntimeSelection(RuntimeTargetOS.DARWIN) if fault == "runtime" else RUNTIME
    with pytest.raises(ValidationError):
        ManagedOperationKeeper(
            receipt,
            ScriptedCarrier(),
            owner=owner,
            obligation_id="b" * 32,
            target=target,
            guest=guest,
            root_plan=plan,
            runtime_selection=runtime,
        )
    assert owner.list_lifecycle_obligations() == ()


@pytest.mark.parametrize("fault", ["close", "takeover", "binding", "late"])
def test_change_between_clock_and_fence_prevents_initial_authority(bound, fault, monkeypatch) -> None:
    database, owner, receipt = bound
    now = time.monotonic()
    deadline = Deadline(now + 1)

    def response(request: LeaseRequest) -> bytes:
        if fault == "close":
            owner.stop_admission()
        elif fault == "takeover":
            OperationOwner.recover(database.operations, owner.ownership, "f" * 32)
        elif fault == "binding":
            database._operation_connection_for_repository().execute(
                "UPDATE lifecycle_obligations SET payload = ? WHERE obligation_id = ?", (b"wrong", "b" * 32)
            )
        else:
            monkeypatch.setattr(time, "monotonic", lambda: now + 2)
        return _success(request)

    carrier = ScriptedCarrier(response)
    keeper = make_keeper(owner, receipt, carrier)
    try:
        keeper.admit()
        if fault == "late":
            assert keeper.sample_initial(deadline).lease is None
        else:
            with pytest.raises(StateError):
                keeper.sample_initial(deadline)
        assert carrier.calls == 1 and keeper.last_clock is not None
        assert keeper.obligation is not None
    finally:
        monkeypatch.undo()
        assert keeper.drain(Deadline.after(1)).drained


def test_interrupted_registration_and_arming_retain_exact_object(bound, monkeypatch) -> None:
    database, owner, receipt = bound
    keeper = make_keeper(owner, receipt, ScriptedCarrier())
    original = owner._repository.mark_lifecycle_obligation_possible_effect
    error = KeyboardInterrupt()

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise error

    monkeypatch.setattr(owner._repository, "mark_lifecycle_obligation_possible_effect", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            keeper.admit()
        assert caught.value is error and keeper.admission_uncertain and keeper.obligation is not None
        assert owner.list_lifecycle_obligations()[0].state is LifecycleObligationState.POSSIBLE_EFFECT
        with pytest.raises(StateError):
            keeper.admit()
    finally:
        assert keeper.drain(Deadline.after(1)).drained


@pytest.mark.parametrize("fault", ["uncertain", "retained", "wrong_run", "wrong_launch", "no_sample"])
def test_only_exact_clean_start_can_enable_one_worker(bound, fault) -> None:
    _, owner, receipt = bound
    carrier = ScriptedCarrier()
    keeper = make_keeper(owner, receipt, carrier)
    outcome = clean_start(receipt)
    assert outcome.attempt is not None
    attempt = outcome.attempt
    if fault == "uncertain":
        outcome = replace(outcome, coordination_uncertain=True)
    elif fault == "retained":
        outcome = replace(outcome, requires_owner_retention=True)
    elif fault == "wrong_run":
        from agentworks.execution._managed_runs import ManagedRunIdentity

        outcome = replace(
            outcome,
            attempt=replace(attempt, record=replace(attempt.record, identity=ManagedRunIdentity("f" * 32))),
        )
    elif fault == "wrong_launch":
        outcome = replace(
            outcome,
            attempt=replace(
                attempt,
                candidate=replace(
                    attempt.candidate,
                    observation=ManagedStartObservation(ManagedStartState.ACKNOWLEDGED, launch_fact=b"wrong"),
                ),
            ),
        )
    try:
        keeper.admit()
        if fault != "no_sample":
            keeper.sample_initial(Deadline.after(1))
        with pytest.raises(StateError):
            keeper.acknowledge_start(outcome)
        assert keeper._worker is None
        with pytest.raises(StateError):
            keeper.acknowledge_start(clean_start(receipt))
    finally:
        assert keeper.drain(Deadline.after(1)).drained


@pytest.mark.parametrize("fault", ["none", "unknown", "interrupt", "unsettled", "late", "close", "takeover"])
def test_one_worker_uses_same_cycle_deadline_store_and_no_replay(bound, fault, monkeypatch) -> None:
    database, owner, receipt = bound
    carrier = ScriptedCarrier()
    deadlines: list[Deadline] = []
    custodies = []
    original_execute = carrier.execute

    def execute(invocation, *, io, deadline, custody):
        deadlines.append(deadline)
        custodies.append(custody)
        return original_execute(invocation, io=io, deadline=deadline, custody=custody)

    monkeypatch.setattr(carrier, "execute", execute)
    keeper = make_keeper(owner, receipt, carrier)

    def response(request: LeaseRequest) -> bytes:
        if carrier.calls == 2 and fault in {"close", "takeover"}:
            if fault == "close":
                owner.stop_admission()
            else:
                OperationOwner.recover(database.operations, owner.ownership, "f" * 32)
        if request.lease is not None:
            if fault == "unknown":
                carrier.dispatch = Dispatch.UNKNOWN
            elif fault == "interrupt":
                carrier.interrupt = KeyboardInterrupt()
            elif fault == "unsettled":
                carrier.unsettled = True
            elif fault == "late":
                monkeypatch.setattr(time, "monotonic", lambda: deadline_time + 10)
            else:
                keeper._stop.set()
        return _success(request)

    try:
        keeper.admit()
        keeper.sample_initial(Deadline.after(1))
        carrier.response = response
        monkeypatch.setattr(keeper_module, "_CADENCE_SECONDS", 0)
        deadline_time = time.monotonic()
        borrow = owner.borrow()
        attempt = borrow.begin_attempt()
        keeper.acknowledge_start(clean_start(receipt))
        worker = keeper._worker
        assert worker is not None
        assert keeper._worker_done.wait(1)
        assert carrier.calls == (2 if fault in {"close", "takeover"} else 3)
        assert all(custody is custodies[0] for custody in custodies)
        if carrier.calls == 3:
            assert deadlines[1] is deadlines[2]
            assert deadlines[1].expires_at is not None
            assert carrier.requests[1].nonce != carrier.requests[2].nonce
            assert carrier.requests[2].expected_launch == encode_managed_job_fact(receipt)
            assert keeper.publication_uncertain == (fault != "none")
        with pytest.raises(StateError):
            keeper.acknowledge_start(clean_start(receipt))
        if fault != "takeover":
            attempt.settle()
            borrow.close()
    finally:
        monkeypatch.undo()
        assert keeper.drain(Deadline.after(1)).drained


def test_blocked_database_fence_drain_does_not_acquire_owner_guard(bound, monkeypatch) -> None:
    database, owner, receipt = bound
    keeper = make_keeper(owner, receipt, ScriptedCarrier())
    entered, release = threading.Event(), threading.Event()
    original = owner._repository.mark_lifecycle_obligation_possible_effect

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    try:
        keeper.admit()
        keeper.sample_initial(Deadline.after(1))
        monkeypatch.setattr(owner._repository, "mark_lifecycle_obligation_possible_effect", blocked)
        monkeypatch.setattr(keeper_module, "_CADENCE_SECONDS", 0)
        keeper.acknowledge_start(clean_start(receipt))
        assert entered.wait(1)
        owner.stop_admission()
        start = time.monotonic()
        facts = keeper.drain(Deadline.after(0.02))
        assert facts.worker_active and not facts.drained and time.monotonic() - start < 0.5
        with pytest.raises(StateError):
            keeper.request_stop(Deadline.after(1))
        assert keeper.obligation is not None
    finally:
        release.set()
        assert keeper.drain(Deadline.after(1)).drained


def test_interrupted_native_thread_start_denies_late_worker(bound, monkeypatch) -> None:
    _, owner, receipt = bound
    carrier = ScriptedCarrier()
    keeper = make_keeper(owner, receipt, carrier)
    original = threading.Thread.start
    error = KeyboardInterrupt()

    def interrupted(thread):
        original(thread)
        raise error

    try:
        keeper.admit()
        keeper.sample_initial(Deadline.after(1))
        monkeypatch.setattr(threading.Thread, "start", interrupted)
        with pytest.raises(KeyboardInterrupt) as caught:
            keeper.acknowledge_start(clean_start(receipt))
        assert caught.value is error and keeper._worker is not None
        assert keeper._worker_done.wait(1)
        assert carrier.calls == 1 and keeper.failure is error
    finally:
        assert keeper.drain(Deadline.after(1)).drained


def test_failed_thread_start_retains_pending_start_and_refuses_cleanup(bound, monkeypatch) -> None:
    _, owner, receipt = bound
    carrier = ScriptedCarrier()
    keeper = make_keeper(owner, receipt, carrier)
    error = RuntimeError("native start not acknowledged")

    def failed(thread):
        raise error

    keeper.admit()
    keeper.sample_initial(Deadline.after(1))
    monkeypatch.setattr(threading.Thread, "start", failed)
    with pytest.raises(RuntimeError) as caught:
        keeper.acknowledge_start(clean_start(receipt))
    assert caught.value is error
    facts = keeper.drain(Deadline.after(0.01))
    assert facts.startup_pending and not facts.worker_active and not facts.drained
    with pytest.raises(StateError):
        keeper.observe_cleanup(Deadline.after(1))
    # Simulate the delayed native entry: permission is already denied.
    keeper._renew()
    assert keeper.drain(Deadline.after(1)).drained and carrier.calls == 1


def test_cadence_uses_actual_cycle_start_without_catch_up(bound, monkeypatch) -> None:
    _, owner, receipt = bound
    carrier = ScriptedCarrier()
    keeper = make_keeper(owner, receipt, carrier)
    clock = [0.0]
    waits: list[float] = []
    cycle_deadlines: list[Deadline] = []

    class ControlledStop:
        def is_set(self):
            return False

        def set(self):
            pass

        def wait(self, seconds):
            waits.append(seconds)
            if len(waits) == 1:
                clock[0] = 50.0  # The host resumes after missing several nominal ticks.
            elif len(waits) == 2:
                clock[0] += seconds
            return len(waits) == 3

    def response(request):
        if request.lease is not None:
            assert carrier.deadline is not None
            cycle_deadlines.append(carrier.deadline)
            clock[0] += 0.2
        return _success(request)

    try:
        keeper.admit()
        keeper.sample_initial(Deadline.after(1))
        carrier.response = response
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(keeper, "_stop", ControlledStop())
        keeper._worker_permission = True
        keeper._startup_decided.set()
        keeper._renew()
        assert waits == pytest.approx([10, 9.8, 9.8])
        assert [deadline.expires_at for deadline in cycle_deadlines] == [55, 65]
        assert carrier.calls == 5 and keeper.failure is None
    finally:
        monkeypatch.undo()
        assert keeper.drain(Deadline.after(1)).drained


@pytest.mark.parametrize("fault", ["close", "takeover", "local_close"])
def test_publication_validation_cannot_bypass_last_delivery_fence(bound, monkeypatch, fault) -> None:
    database, owner, receipt = bound
    carrier = ScriptedCarrier()
    keeper = make_keeper(owner, receipt, carrier)
    original = carrier.validate

    def validate(invocation, *, io):
        original(invocation, io=io)
        if carrier.validations == 3:
            if fault == "takeover":
                OperationOwner.recover(database.operations, owner.ownership, "f" * 32)
            elif fault == "close":
                owner.stop_admission()
            else:
                keeper._stop.set()

    try:
        keeper.admit()
        keeper.sample_initial(Deadline.after(1))
        monkeypatch.setattr(carrier, "validate", validate)
        monkeypatch.setattr(keeper_module, "_CADENCE_SECONDS", 0)
        keeper.acknowledge_start(clean_start(receipt))
        assert keeper._worker_done.wait(1)
        assert carrier.calls == 2 and keeper.failure is not None
        assert keeper.last_publication is None
    finally:
        assert keeper.drain(Deadline.after(1)).drained


def test_uncertain_registration_retains_preheld_identity_without_replay(bound, monkeypatch) -> None:
    _, owner, receipt = bound
    carrier = ScriptedCarrier()
    keeper = make_keeper(owner, receipt, carrier)
    original = owner.register_lifecycle_obligation
    error = KeyboardInterrupt()

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise error

    try:
        monkeypatch.setattr(owner, "register_lifecycle_obligation", interrupted)
        with pytest.raises(KeyboardInterrupt) as caught:
            keeper.admit()
        assert caught.value is error and keeper.failure is error
        assert keeper.obligation is None and keeper.admission_uncertain
        assert owner.list_lifecycle_obligations()[0].obligation_id == "b" * 32
        with pytest.raises(StateError):
            keeper.admit()
        assert carrier.calls == 0
    finally:
        assert keeper.drain(Deadline.after(1)).drained
