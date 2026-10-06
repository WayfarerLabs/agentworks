"""Bounded lifetime rows and exact interrupted inline bookkeeping."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from threading import Thread
from uuid import uuid4

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._execution_operation import ExecutionOperation
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
        rows = database.operations.list_lifecycle_obligations(owner.ownership)
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
    assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
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
    (before,) = database.operations.list_lifecycle_obligations(owner.ownership)
    assert before.state is LifecycleObligationState.POSSIBLE_EFFECT
    clean = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    assert not _run(operation, clean).requires_owner_retention
    (after,) = database.operations.list_lifecycle_obligations(owner.ownership)
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
    retained_ids = {row.obligation_id for row in database.operations.list_lifecycle_obligations(owner.ownership)}
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
    rows = database.operations.list_lifecycle_obligations(owner.ownership)
    assert {row.obligation_id for row in rows} == retained_ids
    assert all(row.state is LifecycleObligationState.RESOLVED for row in rows)
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
    assert database.operations.list_lifecycle_obligations(owner.ownership) == ()
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
    assert database.operations.list_lifecycle_obligations(owner.ownership)[0].state is LifecycleObligationState.RESOLVED


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
    assert database.operations.list_lifecycle_obligations(owner.ownership)[0].state is LifecycleObligationState.RESOLVED


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
    (row,) = database.operations.list_lifecycle_obligations(owner.ownership)
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
    rows = database.operations.list_lifecycle_obligations(recovery.ownership)
    assert len(rows) == 1 and rows[0].state is LifecycleObligationState.POSSIBLE_EFFECT
