"""Actual private leased start composition with framed helpers and real SQLite."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _managed_operation_keeper as keeper_module
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_operation_run import ManagedOperationRun
from agentworks.execution._managed_request_adapter import _ManagedBody, compose_managed_body
from agentworks.execution._managed_runs import (
    ManagedLaunchState,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunRepository,
)
from agentworks.execution._managed_start_protocol import ManagedStartResult, encode_result
from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.execution.models import Command, Input, Output
from agentworks.operations import OperationOwner

from . import test_managed_operation_keeper as keeper_tests
from .test_managed_lease_exchange import PLAN, RUNTIME, ScriptedCarrier, _success
from .test_managed_start_operation import GUEST, RUN, Carrier, _records, _spec

bound = keeper_tests.bound
# Reservation fences, start custody and renewal threads must also run on Windows.
pytestmark = pytest.mark.windows


def body_for(receipt) -> _ManagedBody:
    return compose_managed_body(
        Command(["/usr/bin/true"]),
        input=Input.bytes(b"private-input"),
        output=Output.capture(4096),
        env={"LANG": "C", "TOKEN": "private-env"},
        cwd="/tmp",
        sensitive=False,
        identity=receipt.identity,
        spec=receipt.spec,
    )


def make_run(
    bound, start_carrier=None, clock_carrier=None, **changes
) -> tuple[ManagedOperationRun, ManagedRunRepository, Carrier, ScriptedCarrier]:
    database, owner, receipt = bound
    repository = ManagedRunRepository(database)
    carrier = start_carrier or Carrier(lambda request: _records(request, receipt=True))
    clock = clock_carrier or ScriptedCarrier()
    values = dict(
        owner=owner,
        start_obligation_id="e" * 32,
        keeper_obligation_id="b" * 32,
        target=receipt.spec.target,
        guest=GUEST,
        root_plan=PLAN,
        runtime_selection=RUNTIME,
    )
    values.update(changes)
    return ManagedOperationRun(repository, receipt, carrier, clock, **values), repository, carrier, clock


def test_real_sample_reservation_ack_and_renewal_during_ordinary_attempt(bound, monkeypatch) -> None:
    database, owner, receipt = bound
    permission, entered = threading.Event(), threading.Event()
    clock = ScriptedCarrier()
    requests = []

    def ack(request):
        requests.append(request)
        assert request.job.operation_lease is not None and request.job.operation_lease.sampled_ns == 1000
        assert request.job.environment == (("LANG", "C"), ("TOKEN", "private-env"))
        assert threading.current_thread() is threading.main_thread()
        return _records(request, receipt=True)

    run, repository, start, _ = make_run(bound, Carrier(ack), clock)

    def response(request):
        if clock.calls == 1:
            record = repository.inspect(receipt.identity)
            assert record is not None and record.launch_state is ManagedLaunchState.RESERVED
        elif request.lease is None:
            entered.set()
            assert permission.wait(2)
        else:
            run.keeper._stop.set()
        return _success(request)

    clock.response = response
    deadline = Deadline.after(2)
    try:
        assert repository.inspect(receipt.identity) is None and not run.reservation_started
        monkeypatch.setattr(keeper_module, "_CADENCE_SECONDS", 0)
        outcome = run.start(body_for(receipt), deadline)
        assert outcome is run.start_outcome and outcome is not None and not outcome.requires_owner_retention
        assert run.reserved is not None and run._prepared is None
        assert start.calls == 1 and len(requests) == 1
        assert entered.wait(1)
        borrow = owner.borrow()
        attempt = borrow.begin_attempt()
        changes = database._operation_connection_for_repository().total_changes
        permission.set()
        assert run.keeper._worker_done.wait(1)
        assert database._operation_connection_for_repository().total_changes == changes
        assert run.keeper.initial is not None
        assert clock.calls == 3 and clock.requests[-1].lease == run.keeper.initial.lease
        with pytest.raises(StateError):
            owner.borrow()
        attempt.settle()
        borrow.close()
        rows = owner.list_lifecycle_obligations()
        assert any(
            row.obligation_kind == "managed-operation-keeper" and row.state is LifecycleObligationState.POSSIBLE_EFFECT
            for row in rows
        )
        assert any(
            row.obligation_kind == "managed-start" and row.state is LifecycleObligationState.RESOLVED for row in rows
        )
        with pytest.raises(StateError):
            run.start(body_for(receipt), Deadline.after(1))
    finally:
        permission.set()
        assert run.keeper.drain(Deadline.after(1)).drained


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("inspect_failed", [False, True])
def test_uncertain_reserve_inspects_planned_id_once_and_never_starts(
    bound, monkeypatch, committed, inspect_failed
) -> None:
    _, _, receipt = bound
    run, repository, start, clock = make_run(bound)
    reserve, inspect = repository.reserve, repository.inspect
    error = KeyboardInterrupt()
    inspections = []

    def interrupted(*args, **kwargs):
        if committed:
            reserve(*args, **kwargs)
        raise error

    def observe(identity):
        inspections.append(identity)
        if inspect_failed:
            raise OSError("database observation unavailable")
        return inspect(identity)

    try:
        monkeypatch.setattr(repository, "reserve", interrupted)
        monkeypatch.setattr(repository, "inspect", observe)
        with pytest.raises(KeyboardInterrupt) as caught:
            run.start(body_for(receipt), Deadline.after(1))
        assert caught.value is error and run.control_escaped and run.reservation_started
        assert inspections == [receipt.identity]
        assert run.reservation_uncertain is inspect_failed
        assert (run.reserved is not None) == (committed and not inspect_failed)
        assert not run.keeper.registration_started and start.calls == 0 and clock.calls == 0
        with pytest.raises(StateError):
            run.start(body_for(receipt), Deadline.after(1))
        assert inspections == [receipt.identity]
    finally:
        assert run.keeper.drain(Deadline.after(1)).drained


@pytest.mark.parametrize("fault", ["environment", "cwd", "output", "control"])
def test_invalid_body_preparation_has_no_reservation_or_clock_effect(bound, fault) -> None:
    _, owner, receipt = bound
    run, repository, start, clock = make_run(bound)
    try:
        with pytest.raises(ValidationError):
            compose_managed_body(
                Command(["x" * 4000] * 9) if fault == "control" else Command(["/usr/bin/true"]),
                input=Input.bytes(b""),
                output=Output.capture(1 << 30) if fault == "output" else Output.capture(4096),
                env={"BASH_ENV": "forbidden"} if fault == "environment" else {},
                cwd="relative" if fault == "cwd" else "/tmp",
                sensitive=False,
                identity=receipt.identity,
                spec=receipt.spec,
            )
        assert not run.reservation_started and repository.inspect(receipt.identity) is None
        assert owner.list_lifecycle_obligations() == () and start.calls == clock.calls == 0
    finally:
        assert run.keeper.drain(Deadline.after(1)).drained


def test_start_requires_originating_caller_thread_before_any_effect(bound) -> None:
    run, repository, start, clock = make_run(bound)
    errors = []

    def attempt() -> None:
        try:
            run.start(body_for(bound[2]), Deadline.after(1))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=attempt)
    try:
        worker.start()
        worker.join(1)
        assert not worker.is_alive() and len(errors) == 1 and isinstance(errors[0], StateError)
        assert not run.reservation_started and repository.inspect(bound[2].identity) is None
        assert start.calls == clock.calls == 0
    finally:
        worker.join(1)
        assert not worker.is_alive()
        assert run.keeper.drain(Deadline.after(1)).drained


def test_non_main_originating_caller_owns_database_and_actual_start(tmp_path: Path) -> None:
    errors = []
    completed = []

    def caller() -> None:
        database = Database(tmp_path / "state.db")
        try:
            assert threading.current_thread() is not threading.main_thread()
            owner = OperationOwner.acquire(
                database.operations, OperationScope(OperationResourceKind.VM, "vm-one"), "caller"
            )
            spec = replace(
                _spec(),
                lifetime=ManagedRunLifetime.OPERATION,
                owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, owner.ownership.operation_id),
            )
            receipt = ManagedRunReceipt(RUN, RUN.unit_name, spec)
            caller_id = threading.get_ident()

            def ack(request):
                assert threading.get_ident() == caller_id
                assert request.job.operation_lease is not None
                return _records(request, receipt=True)

            run, repository, start, clock = make_run((database, owner, receipt), Carrier(ack))
            try:
                outcome = run.start(body_for(receipt), Deadline.after(1))
                assert outcome is not None and not outcome.requires_owner_retention
                assert run.reserved is not None and run.keeper._worker is not None
                observed = repository.inspect(RUN)
                assert observed is not None and observed.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
                assert start.calls == 1 and clock.calls >= 1
                completed.append(True)
            finally:
                assert run.keeper.drain(Deadline.after(1)).drained
        except BaseException as error:
            errors.append(error)
        finally:
            database.close()

    worker = threading.Thread(target=caller)
    try:
        worker.start()
        worker.join(2)
        assert not worker.is_alive() and not errors and completed == [True]
    finally:
        worker.join(2)
        assert not worker.is_alive()


def test_post_reserve_carrier_refusal_retains_tombstone_and_clock(bound, monkeypatch) -> None:
    _, _, receipt = bound
    run, repository, start, clock = make_run(bound)

    def refuse(*args, **kwargs):
        raise ValidationError("carrier cannot fit complete leased envelope")

    try:
        monkeypatch.setattr(start, "validate", refuse)
        with pytest.raises(ValidationError):
            run.start(body_for(receipt), Deadline.after(1))
        assert run.reserved is not None and run.reserved.launch_state is ManagedLaunchState.RESERVED
        assert repository.inspect(receipt.identity) == run.reserved
        assert run.keeper.initial is not None and run.keeper.initial.lease is not None
        assert clock.calls == 1 and start.calls == 0 and run._prepared is None
        assert (
            run.keeper.obligation is not None
            and run.keeper.obligation.state is LifecycleObligationState.POSSIBLE_EFFECT
        )
    finally:
        assert run.keeper.drain(Deadline.after(1)).drained


@pytest.mark.parametrize("fault", ["unknown", "malformed", "interrupt"])
def test_clock_failure_or_interruption_retains_reservation_without_start(bound, fault) -> None:
    _, _, receipt = bound
    clock = ScriptedCarrier()
    if fault == "unknown":
        clock.dispatch = Dispatch.UNKNOWN
    elif fault == "malformed":
        clock.response = lambda request: b"malformed"
    else:
        clock.interrupt = KeyboardInterrupt()
    run, _, start, _ = make_run(bound, clock_carrier=clock)
    try:
        if fault == "interrupt":
            with pytest.raises(KeyboardInterrupt) as caught:
                run.start(body_for(receipt), Deadline.after(1))
            assert caught.value is clock.interrupt
        else:
            assert run.start(body_for(receipt), Deadline.after(1)) is None
            assert run.keeper.last_clock is not None
        assert run.reserved is not None and start.calls == 0 and run.keeper._worker is None
    finally:
        assert run.keeper.drain(Deadline.after(1)).drained


@pytest.mark.parametrize("phase", ["clock", "prepare", "armed"])
@pytest.mark.parametrize("fault", ["close", "takeover"])
def test_close_or_takeover_between_clock_and_start_refuses_body(bound, monkeypatch, phase, fault) -> None:
    database, owner, receipt = bound
    run, _, start, clock = make_run(bound)

    def fence_change() -> None:
        if fault == "close":
            owner.stop_admission()
        else:
            OperationOwner.recover(database.operations, owner.ownership, "f" * 32)

    def response(request):
        fence_change()
        return _success(request)

    if phase == "clock":
        clock.response = response
    elif phase == "prepare":
        monkeypatch.setattr(start, "validate", lambda *args, **kwargs: fence_change())
    try:
        with pytest.raises(StateError):
            run.start(body_for(receipt), Deadline.after(1), before_dispatch=fence_change if phase == "armed" else None)
        assert run.control_escaped and run.reserved is not None and start.calls == 0 and clock.calls == 1
        assert run.keeper._worker is None and run._prepared is None
        if phase == "armed":
            assert run.start_outcome is not None and run.start_outcome.requires_owner_retention
    finally:
        assert run.keeper.drain(Deadline.after(1)).drained


@pytest.mark.parametrize("fault", ["uncertain", "missing", "interrupted"])
def test_uncertain_or_non_ack_start_never_gets_worker(bound, fault) -> None:
    _, _, receipt = bound

    def uncertain(request):
        entries = (
            (FileRecordKind.RESULT, encode_result(ManagedStartResult(1, None, (FactName.LAUNCH,)))),
            (FileRecordKind.DATA, request.job.launch),
            (FileRecordKind.FINISHED, b""),
        )
        return b"".join(
            encode_file_record(request.nonce, FileRecord(index, kind, data))
            for index, (kind, data) in enumerate(entries)
        )

    carrier = Carrier(uncertain if fault == "uncertain" else lambda request: _records(request, receipt=False))
    error = KeyboardInterrupt()
    if fault == "interrupted":
        carrier.error = error
    run, _, _, clock = make_run(bound, carrier)
    try:
        if fault == "interrupted":
            with pytest.raises(KeyboardInterrupt) as caught:
                run.start(body_for(receipt), Deadline.after(1))
            assert caught.value is error and run.control_escaped
        else:
            assert run.start(body_for(receipt), Deadline.after(1)) is run.start_outcome
        assert run.start_outcome is not None and carrier.calls == 1 and clock.calls == 1
        assert run.keeper._worker is None and run._prepared is None
    finally:
        assert run.keeper.drain(Deadline.after(1)).drained


def test_ack_handoff_startup_refusal_preserves_completed_start_evidence(bound, monkeypatch) -> None:
    _, _, receipt = bound
    run, _, start, clock = make_run(bound)
    error = RuntimeError("native creation refused")

    def refused(*args, **kwargs):
        raise error

    try:
        spawn = "_start_joinable_thread" if hasattr(threading, "_start_joinable_thread") else "_start_new_thread"
        monkeypatch.setattr(threading, spawn, refused)
        with pytest.raises(RuntimeError) as caught:
            run.start(body_for(receipt), Deadline.after(1))
        assert caught.value is error and run.start_outcome is not None
        assert run.start_outcome.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
        assert not run.start_outcome.requires_owner_retention
        assert start.calls == 1 and clock.calls == 1 and run._prepared is None
        assert (
            run.keeper.obligation is not None
            and run.keeper.obligation.state is LifecycleObligationState.POSSIBLE_EFFECT
        )
    finally:
        assert run.keeper.drain(Deadline.after(1)).drained


@pytest.mark.parametrize("fault", ["owner", "boot", "shared_carrier", "body"])
def test_wrong_binding_refuses_before_reservation_and_clock(bound, fault) -> None:
    _, owner, receipt = bound
    options = {}
    if fault == "owner":
        receipt = replace(
            receipt, spec=replace(receipt.spec, owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "f" * 32))
        )
        bound = (bound[0], owner, receipt)
    elif fault == "boot":
        options["guest"] = replace(GUEST, init_start_ticks=GUEST.init_start_ticks + 1)
        assert vm_guest_boot_id(options["guest"]) != receipt.spec.target.boot_id
    if fault in {"owner", "boot", "shared_carrier"}:
        carrier = ScriptedCarrier()
        with pytest.raises(ValidationError):
            make_run(
                bound, start_carrier=carrier, clock_carrier=carrier if fault == "shared_carrier" else None, **options
            )
        assert owner.list_lifecycle_obligations() == ()
        return
    run, repository, start, clock = make_run(bound)
    wrong = replace(
        receipt, identity=type(receipt.identity)("f" * 32), unit_name=type(receipt.identity)("f" * 32).unit_name
    )
    try:
        with pytest.raises(ValidationError):
            run.start(body_for(wrong), Deadline.after(1))
        assert repository.inspect(receipt.identity) is None and not run.reservation_started
        assert start.calls == 0 and clock.calls == 0
    finally:
        assert run.keeper.drain(Deadline.after(1)).drained
