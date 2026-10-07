"""Bounded lifetime rows and exact interrupted inline bookkeeping."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from uuid import uuid4

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._execution_operation import ExecutionOperation, InlineExecutionControlFact
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.access import ExecutionAccess
from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, Dispatch, ExitStatus, PreparedInvocation
from agentworks.execution.models import Command, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ExitCode
from agentworks.operations import LifecycleObligation, OperationAttempt, OperationBorrow, OperationOwner
from tests.execution.files._target_support import target_for_owner
from tests.execution.test_execution_access import LocalCarrier
from tests.execution.test_execution_operation import RecordingCarrier, _patch_candidate_execution


@pytest.fixture
def owned(tmp_path: Path):
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations, OperationScope(OperationResourceKind.VM, "lifetime-vm"), "inline"
    )
    operation = ExecutionOperation(owner, target_for_owner(owner))
    try:
        yield database, owner, operation
    finally:
        database.close()


def _run(operation: ExecutionOperation, carrier: RecordingCarrier):
    return operation.run_inline(
        carrier,
        Command(("/bin/true",)),
        plan=IdentityPlan(IdentityExpectation(1, 1, (1,)), IdentityMode.DIRECT),
        deadline=Deadline.after(30),
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable),
    )


@pytest.mark.skipif(sys.platform != "linux", reason="actual inline helpers require Linux")
def test_actual_mixed_calls_share_one_lifetime_row(owned, tmp_path: Path) -> None:
    database, owner, operation = owned
    carrier = LocalCarrier()
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    plan = IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)
    runtime = RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable)
    access = ExecutionAccess(
        operation,
        carrier,
        runtime_selection=runtime,
        ordinary_plan=plan,
        elevated_plan=plan,
        entity_kind="vm",
        entity_name="lifetime-vm",
        deadline=lambda: Deadline.after(30),
    )
    files = FileOperation(owner, target_for_owner(owner))
    dispatch_ids = set()
    file_calls = 0
    for index in range(160):
        request = Command(("/bin/true",)) if index % 2 else Script("exit 7", Shell.SH)
        result = access.run(request, profile=Protection.DIRECT, sudo=bool(index % 2))
        assert result.status == ExitCode(0 if index % 2 else 7)
        assert result.owned_cleanup_confirmed
        if index % 20 == 0:
            observed = files.stat(
                carrier,
                trusted_root_path=str(tmp_path),
                relative_path="missing",
                plan=plan,
                deadline=Deadline.after(30),
                runtime_selection=runtime,
            )
            assert not observed.requires_owner_retention
            file_calls += 1
        rows = database.operations.list_pending_lifecycle_obligations(owner.ownership)
        dispatch = [row for row in rows if row.obligation_kind == "carrier-dispatch"]
        assert len(dispatch) == 1
        dispatch_ids.add(dispatch[0].obligation_id)
        assert dispatch[0].state is LifecycleObligationState.POSSIBLE_EFFECT
    assert len(dispatch_ids) == 1
    assert carrier.calls == 160 + file_calls
    operation.finish()
    operation.finish()
    owner.seal_lifecycle_obligations()
    owner.record_effects_resolved()
    owner.close()


def test_unused_finish_and_post_finish_refusal(owned) -> None:
    database, owner, operation = owned
    operation.finish()
    operation.finish()
    assert database.operations.list_pending_lifecycle_obligations(owner.ownership) == ()
    carrier = RecordingCarrier()
    with pytest.raises(StateError):
        _run(operation, carrier)
    assert carrier.calls == 0
    owner.close()


def test_validation_refusal_preserves_lifetime_row_for_next_call(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    database, owner, operation = owned
    _patch_candidate_execution(monkeypatch)

    class RefusingCarrier(RecordingCarrier):
        def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
            raise ValidationError("refused")

    refused = RefusingCarrier()
    with pytest.raises(ValidationError):
        _run(operation, refused)
    (before,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    assert before.state is LifecycleObligationState.POSSIBLE_EFFECT
    clean = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    assert not _run(operation, clean).requires_owner_retention
    (after,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    assert after.obligation_id == before.obligation_id
    assert refused.calls == 0 and clean.calls == 1
    operation.finish()


@pytest.mark.parametrize("transition", ["registration", "arming", "settlement", "handoff"])
@pytest.mark.parametrize("finish", [False, True])
def test_lost_bookkeeping_reply_never_replays_helper(
    owned, monkeypatch: pytest.MonkeyPatch, transition: str, finish: bool
) -> None:
    database, owner, operation = owned
    _patch_candidate_execution(monkeypatch)
    if transition == "registration":
        target, name = type(database.operations), "register_lifecycle_obligation"
    elif transition == "arming":
        target, name = type(database.operations), "mark_lifecycle_obligation_possible_effect"
    elif transition == "settlement":
        target, name = OperationAttempt, "settle"
    else:
        target, name = OperationBorrow, "handoff_retained_effect"
    original = getattr(target, name)
    calls = 0

    def lost_reply(self, *args, **kwargs):
        nonlocal calls
        result = original(self, *args, **kwargs)
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("reply lost")
        return result

    monkeypatch.setattr(target, name, lost_reply)
    carrier = RecordingCarrier(CarrierReport(Dispatch.SENT, ExitStatus(code=0)))
    with pytest.raises(KeyboardInterrupt):
        _run(operation, carrier)
    retained_ids = {
        row.obligation_id for row in database.operations.list_pending_lifecycle_obligations(owner.ownership)
    }
    assert len(operation.active_inline_calls) == 1
    dispatched_calls = 1 if transition in {"settlement", "handoff"} else 0
    assert carrier.calls == dispatched_calls
    if finish:
        owner.stop_admission()
        operation.finish()
    else:
        operation.retry_inline_bookkeeping()
        assert operation.active_inline_calls == ()
        assert carrier.calls == dispatched_calls
        assert not _run(operation, carrier).requires_owner_retention
        operation.finish()
    rows = [owner.inspect_lifecycle_obligation(identifier) for identifier in retained_ids]
    assert all(row is not None and row.state is LifecycleObligationState.RESOLVED for row in rows)
    assert owner.list_pending_lifecycle_obligations() == ()
    assert carrier.calls == dispatched_calls + (0 if finish else 1)


def test_finish_recovers_registration_failure_without_creating_row(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    database, owner, operation = owned
    _patch_candidate_execution(monkeypatch)

    def fail(self, *args, **kwargs):
        raise KeyboardInterrupt("not committed")

    monkeypatch.setattr(type(database.operations), "register_lifecycle_obligation", fail)
    carrier = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    with pytest.raises(KeyboardInterrupt):
        _run(operation, carrier)
    owner.stop_admission()
    operation.finish()
    assert carrier.calls == 0
    assert operation.active_inline_calls == ()
    assert database.operations.list_pending_lifecycle_obligations(owner.ownership) == ()
    owner.close()


def test_finish_resolution_reply_loss_retries_exact_id(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    database, owner, operation = owned
    _patch_candidate_execution(monkeypatch)
    carrier = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    _run(operation, carrier)
    original = LifecycleObligation.resolve
    ids = []

    def lost_reply(self):
        ids.append(self.obligation_id)
        original(self)
        if len(ids) == 1:
            raise KeyboardInterrupt("reply lost")

    monkeypatch.setattr(LifecycleObligation, "resolve", lost_reply)
    with pytest.raises(KeyboardInterrupt):
        operation.finish()
    operation.finish()
    assert len(ids) == 2 and len(set(ids)) == 1
    assert carrier.calls == 1
    assert operation._dispatch_id is not None
    receipt = owner.inspect_lifecycle_obligation(operation._dispatch_id)
    assert receipt is not None and receipt.state is LifecycleObligationState.RESOLVED


@pytest.mark.parametrize("finish", [False, True])
def test_lost_begin_attempt_reply_has_no_helper_to_replay(owned, monkeypatch: pytest.MonkeyPatch, finish: bool) -> None:
    database, owner, operation = owned
    _patch_candidate_execution(monkeypatch)
    original = OperationBorrow.begin_attempt

    def lost_reply(self):
        original(self)
        raise KeyboardInterrupt("permission reply lost")

    monkeypatch.setattr(OperationBorrow, "begin_attempt", lost_reply)
    carrier = RecordingCarrier(CarrierReport(Dispatch.SENT, ExitStatus(code=0)))
    with pytest.raises(KeyboardInterrupt):
        _run(operation, carrier)
    assert carrier.calls == 0
    assert len(operation.active_inline_calls) == 1
    if finish:
        owner.stop_admission()
        operation.finish()
    else:
        operation.retry_inline_bookkeeping()
        operation.finish()
    assert operation.active_inline_calls == ()
    assert operation._dispatch_id is not None
    receipt = owner.inspect_lifecycle_obligation(operation._dispatch_id)
    assert receipt is not None and receipt.state is LifecycleObligationState.RESOLVED


def test_handoff_reply_loss_does_not_mutate_successor_borrow(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    _, owner, operation = owned
    _patch_candidate_execution(monkeypatch)
    original = OperationBorrow.handoff_retained_effect

    def lost_reply(self):
        original(self)
        raise KeyboardInterrupt("handoff reply lost")

    monkeypatch.setattr(OperationBorrow, "handoff_retained_effect", lost_reply)
    with pytest.raises(KeyboardInterrupt):
        _run(operation, RecordingCarrier(CarrierReport(Dispatch.NOT_SENT)))
    successor = owner.borrow()
    attempt = successor.begin_attempt()
    with pytest.raises(StateError):
        operation.finish()
    assert successor.has_outstanding_attempt
    assert operation.active_inline_calls == ()
    attempt.settle()
    successor.close()
    operation.finish()


@pytest.mark.parametrize("control", [False, True])
def test_unknown_helper_blocks_new_calls_and_finish(owned, monkeypatch: pytest.MonkeyPatch, control: bool) -> None:
    _, owner, operation = owned
    _patch_candidate_execution(monkeypatch)
    carrier = RecordingCarrier(CarrierReport(Dispatch.UNKNOWN), control=KeyboardInterrupt() if control else None)
    if control:
        with pytest.raises(KeyboardInterrupt):
            _run(operation, carrier)
    else:
        assert _run(operation, carrier).requires_owner_retention
    with pytest.raises(StateError):
        operation.finish()
    with pytest.raises(StateError):
        _run(operation, carrier)
    with pytest.raises(StateError):
        owner.borrow()
    assert carrier.calls == 1


def test_finish_racing_active_call_closes_admission_without_resolving(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    database, owner, operation = owned
    _patch_candidate_execution(monkeypatch)
    refusals = []

    def finish() -> None:
        try:
            operation.finish()
        except StateError as error:
            refusals.append(error)

    def during_dispatch() -> None:
        thread = Thread(target=finish)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()

    carrier = RecordingCarrier(CarrierReport(Dispatch.SENT, ExitStatus(code=0)), before_dispatch=during_dispatch)
    assert not _run(operation, carrier).requires_owner_retention
    assert len(refusals) == 1
    (row,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    with pytest.raises(StateError):
        _run(operation, carrier)
    operation.finish()


def test_stale_takeover_prevents_lifetime_resolution(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    database, owner, operation = owned
    _patch_candidate_execution(monkeypatch)
    _run(operation, RecordingCarrier(CarrierReport(Dispatch.NOT_SENT)))
    recovery = OperationOwner.recover(database.operations, owner.ownership, uuid4().hex)
    with pytest.raises(StateError):
        operation.finish()
    rows = database.operations.list_pending_lifecycle_obligations(recovery.ownership)
    assert len(rows) == 1 and rows[0].state is LifecycleObligationState.POSSIBLE_EFFECT


@pytest.mark.parametrize("cleanup", ["finish", "retry"])
@pytest.mark.parametrize("terminal", ["control", "capture_failure"])
def test_terminal_capture_serializes_concurrent_bookkeeping(
    owned, monkeypatch: pytest.MonkeyPatch, cleanup: str, terminal: str
) -> None:
    _, _, operation = owned
    _patch_candidate_execution(monkeypatch)
    paused = Event()
    resume = Event()
    cleanup_entered = Event()
    cleanup_acquired = []
    controls = []
    cleanup_errors = []
    control = ValidationError("validation refusal") if terminal == "control" else KeyboardInterrupt("handoff reply")

    class ObservedGuard:
        """Expose the cleanup thread's first acquisition without timed guesses."""

        def __init__(self) -> None:
            self.lock = Lock()

        def __enter__(self):
            if current_thread().name == "inline-cleanup":
                acquired = self.lock.acquire(blocking=False)
                cleanup_acquired.append(acquired)
                cleanup_entered.set()
                if not acquired:
                    self.lock.acquire()
            else:
                self.lock.acquire()
            return self

        def __exit__(self, *args):
            self.lock.release()

    monkeypatch.setattr(operation, "_admission_guard", ObservedGuard())
    carrier: RecordingCarrier
    if terminal == "control":
        original = operation._outcome

        def pause_control(*args, **kwargs):
            if not kwargs["include_candidate"]:
                paused.set()
                assert resume.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr(operation, "_outcome", pause_control)

        class RefusingCarrier(RecordingCarrier):
            def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
                raise control

        carrier = RefusingCarrier()
    else:
        original_handoff = OperationBorrow.handoff_retained_effect

        def pause_failed_capture(self):
            original_handoff(self)
            if current_thread().name == "inline-caller":
                paused.set()
                assert resume.wait(5)
                raise control

        monkeypatch.setattr(OperationBorrow, "handoff_retained_effect", pause_failed_capture)
        carrier = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))

    def run() -> None:
        try:
            _run(operation, carrier)
        except BaseException as error:
            controls.append(error)

    def reconcile() -> None:
        try:
            if cleanup == "finish":
                operation.finish()
            else:
                operation.retry_inline_bookkeeping()
        except BaseException as error:
            cleanup_errors.append(error)

    caller = Thread(target=run, name="inline-caller")
    cleaner = Thread(target=reconcile, name="inline-cleanup")
    caller.start()
    try:
        assert paused.wait(5)
        cleaner.start()
        assert cleanup_entered.wait(5)
        # The terminal phase retains exclusive custody until it publishes its
        # outcome or failed-capture flag, so cleanup cannot consume it early.
        assert cleanup_acquired == [False]
    finally:
        resume.set()
        caller.join(5)
        if cleaner.ident is not None:
            cleaner.join(5)
    assert not caller.is_alive() and not cleaner.is_alive()
    assert controls == [control]
    assert not cleanup_errors
    if terminal == "control":
        assert isinstance(control.__cause__, InlineExecutionControlFact)
        assert not control.__cause__.outcome.requires_owner_retention
    assert operation.active_inline_calls == ()
    assert not operation.unfinished_inline_executions
    assert carrier.calls == (0 if terminal == "control" else 1)
    operation.finish()


def _lose_second_mark(database: Database, monkeypatch: pytest.MonkeyPatch, *, committed: bool) -> None:
    original = type(database.operations).mark_lifecycle_obligation_possible_effect
    marks = 0

    def lost_reply(self, *args, **kwargs):
        nonlocal marks
        marks += 1
        if marks == 2:
            if committed:
                original(self, *args, **kwargs)
            raise KeyboardInterrupt("attempt admission reply lost")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(database.operations), "mark_lifecycle_obligation_possible_effect", lost_reply)


@pytest.mark.parametrize("committed", [False, True])
def test_second_mark_reply_loss_retry_allows_fresh_command(
    owned, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    database, owner, operation = owned
    _patch_candidate_execution(monkeypatch)
    _lose_second_mark(database, monkeypatch, committed=committed)
    carrier = RecordingCarrier(CarrierReport(Dispatch.SENT, ExitStatus(code=0)))
    with pytest.raises(KeyboardInterrupt):
        _run(operation, carrier)
    assert carrier.calls == 0
    (active,) = operation.active_inline_calls
    assert active.borrow.has_outstanding_attempt
    (before,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    operation.retry_inline_bookkeeping()
    assert carrier.calls == 0
    assert operation.active_inline_calls == ()
    assert not _run(operation, carrier).requires_owner_retention
    assert carrier.calls == 1
    (after,) = database.operations.list_pending_lifecycle_obligations(owner.ownership)
    assert before.obligation_id == after.obligation_id
    operation.finish()


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("refusal", ["observation", "stale"])
def test_second_mark_retry_refusal_preserves_exact_custody(
    owned, monkeypatch: pytest.MonkeyPatch, committed: bool, refusal: str
) -> None:
    database, owner, operation = owned
    _patch_candidate_execution(monkeypatch)
    _lose_second_mark(database, monkeypatch, committed=committed)
    carrier = RecordingCarrier(CarrierReport(Dispatch.SENT, ExitStatus(code=0)))
    with pytest.raises(KeyboardInterrupt):
        _run(operation, carrier)
    (active,) = operation.active_inline_calls
    attempt = owner._outstanding_attempt
    assert attempt is not None and active.borrow.has_outstanding_attempt
    if refusal == "stale":
        recovery = OperationOwner.recover(database.operations, owner.ownership, uuid4().hex)
        rows = database.operations.list_pending_lifecycle_obligations(recovery.ownership)
    else:
        original_inspect = type(database.operations).inspect

        def unavailable(self, *args, **kwargs):
            raise StateError("claim observation unavailable")

        monkeypatch.setattr(type(database.operations), "inspect", unavailable)
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    assert operation.active_inline_calls == (active,)
    assert owner._outstanding_attempt is attempt
    assert active.borrow.has_outstanding_attempt
    assert active.operation.coordination_uncertain
    assert not active.borrow._closed
    assert carrier.calls == 0
    if refusal == "stale":
        assert database.operations.list_pending_lifecycle_obligations(recovery.ownership) == rows
    else:
        monkeypatch.setattr(type(database.operations), "inspect", original_inspect)
        operation.retry_inline_bookkeeping()
        assert not _run(operation, carrier).requires_owner_retention
        assert carrier.calls == 1
        operation.finish()


def test_predispatch_retry_refuses_successor_borrow(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    _, owner, operation = owned
    _patch_candidate_execution(monkeypatch)
    original = OperationBorrow.handoff_retained_effect

    def lost_reply(self):
        original(self)
        raise KeyboardInterrupt("validation handoff reply lost")

    class RefusingCarrier(RecordingCarrier):
        def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
            raise ValidationError("refused")

    monkeypatch.setattr(OperationBorrow, "handoff_retained_effect", lost_reply)
    carrier = RefusingCarrier()
    with pytest.raises(ValidationError):
        _run(operation, carrier)
    (active,) = operation.active_inline_calls
    successor = owner.borrow()
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    assert operation.active_inline_calls == (active,)
    assert not successor._closed
    assert carrier.calls == 0
    attempt = successor.begin_attempt()
    attempt.settle()
    successor.close()
    operation.retry_inline_bookkeeping()
    operation.finish()
