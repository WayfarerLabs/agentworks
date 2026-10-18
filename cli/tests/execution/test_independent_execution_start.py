"""RESOURCE launches retain actual start custody, not operation job ownership."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _execution_operation as operation_module
from agentworks.execution._execution_operation import ManagedExecutionControlFact
from agentworks.execution._managed_bound_run import ManagedDeadlineExpired
from agentworks.execution._managed_runs import ManagedLaunchState, ManagedRunIdentity, ManagedRunLifetime
from agentworks.execution.access import ExecutionAccess
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import Deadline, Retention
from agentworks.execution.models import Command, Input, JobRef, Lifetime, Output, Script, Shell
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ApplicationState, CheckedExecutionError, ExecutionFailure, ExitCode
from agentworks.operations import OperationAttempt, OperationBorrow

from .test_independent_execution_reads import RESOURCE
from .test_independent_execution_reads import observer as observer
from .test_managed_default_shell import lookup as lookup


@pytest.fixture
def available(observer):
    operation = observer[2]
    operation._native_binding = replace(
        operation._native_binding, _independent_availability=_IndependentJobAvailability.NO_IDLE_STOP
    )
    return observer


@pytest.fixture
def view(available):
    # Reuse the existing framed shell probe without starting an OP keeper.
    database, _, _, access, main, workflow = available
    return database, workflow, access, main, None


def start(access: ExecutionAccess, **kwargs: Any) -> JobRef:
    return access.start(
        Command(["/bin/true"]),
        profile=Protection.MANAGED,
        lifetime=Lifetime.INDEPENDENT,
        output=Output.discard(),
        **kwargs,
    )


def test_resource_start_has_exact_receipt_and_action_row_without_job_adoption(available):
    database, repository, operation, access, main, workflow = available
    job = start(access)
    record = repository.inspect(ManagedRunIdentity(job.run_id))
    assert record.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    assert record.spec.lifetime is ManagedRunLifetime.INDEPENDENT and record.spec.owner == RESOURCE
    assert operation.managed_runs == () and operation.active_inline_calls == ()
    assert main.start.calls == 1 and main.clock.calls == main.stop.calls == main.dispose.calls == 0
    rows = database._conn.execute(
        "SELECT obligation_kind, state, payload FROM lifecycle_obligations WHERE operation_id = ?",
        (workflow.owner.ownership.operation_id,),
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == "managed-start" and rows[0][1] == LifecycleObligationState.RESOLVED
    assert job.run_id.encode() in rows[0][2]
    result = access.wait(job)
    assert result.status == ExitCode(7) and result.owned_cleanup_confirmed
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert repository.inspect(ManagedRunIdentity(job.run_id)) == record
    assert main.stop.calls == main.dispose.calls == 0


def test_missing_availability_refuses_before_reservation_or_borrow(observer, monkeypatch):
    _, repository, operation, access, main, workflow = observer
    monkeypatch.setattr(repository, "reserve", lambda *args, **kwargs: pytest.fail("reservation"))
    monkeypatch.setattr(workflow.owner, "borrow", lambda: pytest.fail("borrow"))
    with pytest.raises(StateError):
        start(access)
    assert operation.active_inline_calls == () and main.start.calls == 0


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("kind", [OSError, KeyboardInterrupt, SystemExit])
def test_interrupted_reservation_preserves_control_and_never_launches(available, monkeypatch, committed, kind):
    _, repository, operation, access, main, workflow = available
    original = repository.reserve
    control = kind("reservation")
    old_cause = RuntimeError("original cause")
    control.__cause__ = old_cause

    def reserve(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise control

    monkeypatch.setattr(repository, "reserve", reserve)
    with pytest.raises(kind) as caught:
        start(access)
    assert caught.value is control and isinstance(control.__cause__, ManagedExecutionControlFact)
    assert control.__cause__.__cause__ is not None
    assert control.__cause__.__cause__.__cause__ is old_cause
    identity = ManagedRunIdentity(control.__cause__.reference.run_id)
    record = repository.inspect(identity)
    assert (record is not None) is committed
    if committed:
        assert record.launch_state is ManagedLaunchState.RESERVED
    assert operation.active_inline_calls == () and workflow.owner._active_borrow is None
    assert main.start.calls == 0 and workflow.owner.list_pending_lifecycle_obligations() == ()


@pytest.mark.parametrize("checked", [False, True])
def test_foreground_resource_launch_uses_same_result_and_safe_reference(available, checked):
    _, _, operation, access, main, _ = available
    if checked:
        with pytest.raises(CheckedExecutionError) as caught:
            access.run(
                Command(["/bin/false"]),
                profile=Protection.MANAGED,
                lifetime=Lifetime.INDEPENDENT,
                output=Output.discard(),
                check=True,
            )
        result = caught.value.result
        assert caught.value.entity_kind == "vm" and caught.value.entity_name == "vm-one"
    else:
        result = access.run(
            Command(["/bin/false"]),
            profile=Protection.MANAGED,
            lifetime=Lifetime.INDEPENDENT,
            output=Output.discard(),
        )
    assert result.job is not None and result.status == ExitCode(7)
    assert result.stdout.retention is Retention.DISCARDED
    assert main.start.calls == 1 and main.observe.calls == 3
    assert operation.managed_runs == ()


@pytest.mark.parametrize("committed", [False, True])
def test_reconcile_reply_loss_keeps_actual_candidate_then_retries_without_delivery(available, monkeypatch, committed):
    _, repository, operation, access, main, workflow = available
    original = repository.reconcile
    control = KeyboardInterrupt("reconcile")

    def reconcile(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise control

    monkeypatch.setattr(repository, "reconcile", reconcile)
    with pytest.raises(KeyboardInterrupt) as caught:
        start(access)
    assert caught.value is control
    (active,) = operation.active_inline_calls
    assert active.candidate is not None and active.start is not None
    assert isinstance(control.__cause__, ManagedExecutionControlFact)
    job = control.__cause__.reference
    assert active.start.receipt.identity.run_id == job.run_id
    assert workflow.owner._active_borrow is active.borrow
    monkeypatch.setattr(repository, "reconcile", original)
    operation.retry_inline_bookkeeping()
    assert operation.active_inline_calls == () and main.start.calls == 1
    assert repository.inspect(ManagedRunIdentity(job.run_id)).launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    operation.finish()


@pytest.mark.parametrize("invalid", ["check", "deadline"])
def test_invalid_foreground_flags_refuse_before_effects(available, invalid):
    _, _, operation, access, main, _ = available
    kwargs = {"check": 1} if invalid == "check" else {"deadline": Deadline.after(0)}
    with pytest.raises(ValidationError):
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.INDEPENDENT, **kwargs)
    assert main.start.calls == 0 and operation.active_inline_calls == ()


@pytest.mark.parametrize("sensitive", [False, True])
def test_default_shell_freezes_finite_body_once_before_probe(available, lookup, monkeypatch, sensitive):
    _, repository, operation, access, main, _ = available

    class Environment(dict):
        traversals = 0

        def items(self):
            self.traversals += 1
            return super().items()

    environment = Environment(LANG="C")
    original_probe = lookup[0].execute

    def mutate(*args, **kwargs):
        environment["LANG"] = "changed"
        return original_probe(*args, **kwargs)

    monkeypatch.setattr(lookup[0], "execute", mutate)
    saved = []
    response = main.start.response

    def record(request):
        saved.append(request.job)
        return response(request)

    monkeypatch.setattr(main.start, "response", record)
    job = access.start(
        Script("read value", Shell.USER_DEFAULT),
        profile=Protection.MANAGED,
        lifetime=Lifetime.INDEPENDENT,
        env=environment,
        stdin=Input.sensitive(b"private-input") if sensitive else Input.bytes(b"finite-input"),
        output=Output.capture(10),
        cwd="/tmp",
    )
    assert environment.traversals == 1 and saved[0].environment == (("LANG", "C"),)
    assert saved[0].source == b"read value" and saved[0].cwd == "/tmp"
    assert saved[0].stdin == (b"private-input" if sensitive else b"finite-input")
    assert saved[0].output_mode == ("sensitivity-suppressed" if sensitive else "capture")
    persisted = repository.inspect(ManagedRunIdentity(job.run_id))
    assert persisted.spec.shell.resolved_executable == "/usr/bin/bash"
    assert persisted.spec.lifetime is ManagedRunLifetime.INDEPENDENT
    assert operation.active_inline_calls == () and operation.managed_runs == ()


@pytest.mark.parametrize("invalid", ["env", "cwd", "output", "interactive"])
def test_malformed_body_refuses_before_probe_reservation_and_borrow(available, lookup, monkeypatch, invalid):
    _, repository, _, access, main, workflow = available
    kwargs: dict[str, Any] = {"env": {"BAD=KEY": "secret"}} if invalid == "env" else {}
    if invalid == "cwd":
        kwargs["cwd"] = "relative"
    monkeypatch.setattr(repository, "reserve", lambda *args, **kwargs: pytest.fail("reserve"))
    monkeypatch.setattr(workflow.owner, "borrow", lambda: pytest.fail("borrow"))
    with pytest.raises(ValidationError):
        access.start(
            Script("true", Shell.USER_DEFAULT, interactive=invalid == "interactive"),
            profile=Protection.MANAGED,
            lifetime=Lifetime.INDEPENDENT,
            output=Output.capture(2**30) if invalid == "output" else Output.discard(),
            **kwargs,
        )
    assert lookup[0].calls == main.start.calls == 0


@pytest.mark.parametrize("policy", ["capture", "discard", "sensitive"])
@pytest.mark.parametrize("checked", [False, True])
def test_acknowledged_launch_exhausts_same_budget_without_observation_or_lookup(
    available, monkeypatch, policy, checked
):
    _, repository, operation, access, main, _ = available
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    budget = Deadline(101.0)
    selections = []

    def deadline():
        selections.append(budget)
        return budget

    access._deadline = deadline
    original = main.start.response

    def expired(request):
        response = original(request)
        now[0] = 102.0
        return response

    main.start.response = expired
    original_wait = operation.wait_job

    def wait(*args, **kwargs):
        monkeypatch.setattr(repository, "inspect", lambda *args: pytest.fail("post-expiry read"))
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(operation, "wait_job", wait)
    kwargs = dict(
        output=Output.discard() if policy == "discard" else Output.capture(3), sensitive=policy == "sensitive"
    )
    if checked:
        with pytest.raises(CheckedExecutionError) as caught:
            access.run(
                Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.INDEPENDENT, check=True, **kwargs
            )
        result = caught.value.result
    else:
        result = access.run(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.INDEPENDENT, **kwargs)
    assert result.job is not None and result.application_state is ApplicationState.UNKNOWN
    assert result.failure is ExecutionFailure.DEADLINE and result.deadline_exceeded
    retention = {"capture": Retention.CAPTURED, "discard": Retention.DISCARDED, "sensitive": Retention.SUPPRESSED}[
        policy
    ]
    assert result.stdout.retention is result.stderr.retention is retention
    assert selections == [budget] and main.start.calls == 1 and main.observe.calls == main.stop.calls == 0
    assert operation.active_inline_calls == () and operation.managed_runs == ()


def test_cause_bearing_expiry_never_uses_local_acknowledgement_fallback(available, monkeypatch):
    _, _, operation, access, main, _ = available
    control = ManagedDeadlineExpired("unknown observation")
    cause = KeyboardInterrupt("observation")
    control.__cause__ = cause

    def wait(*args, **kwargs):
        raise control

    monkeypatch.setattr(operation, "wait_job", wait)
    with pytest.raises(ManagedDeadlineExpired) as caught:
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.INDEPENDENT)
    assert caught.value is control and control.__cause__.__cause__ is cause
    assert isinstance(control.__cause__, ManagedExecutionControlFact)
    assert control.__cause__.reference is not None and main.start.calls == 1


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("method", ["install_dispatch_obligation", "arm_dispatch_obligation", "begin_attempt", "close"])
@pytest.mark.windows
def test_start_borrow_transition_reply_loss_retains_exact_custody_without_replay(
    available, monkeypatch, committed, method
):
    _, _, operation, access, main, workflow = available
    original = getattr(OperationBorrow, method)
    control = KeyboardInterrupt(method)
    raised = False

    def interrupt(borrow, *args, **kwargs):
        nonlocal raised
        if raised:
            return original(borrow, *args, **kwargs)
        raised = True
        if committed:
            original(borrow, *args, **kwargs)
        raise control

    with patch.object(OperationBorrow, method, interrupt), pytest.raises(KeyboardInterrupt) as caught:
        start(access)
    assert caught.value is control
    calls = main.start.calls
    operation.retry_inline_bookkeeping()
    assert operation.active_inline_calls == () and workflow.owner._active_borrow is None
    assert main.start.calls == calls == (1 if method == "close" else 0)


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.windows
def test_actual_attempt_settlement_reply_loss_keeps_confirmed_receipt_and_helper(available, committed):
    _, repository, operation, access, main, _ = available
    original = OperationAttempt.settle
    control = SystemExit("settle")

    def settle(attempt):
        if committed:
            original(attempt)
        raise control

    with patch.object(OperationAttempt, "settle", settle), pytest.raises(SystemExit) as caught:
        start(access)
    assert caught.value is control
    (active,) = operation.active_inline_calls
    assert active.start is not None and active.candidate is not None
    assert repository.inspect(active.start.receipt.identity).launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
    operation.retry_inline_bookkeeping()
    assert operation.active_inline_calls == () and main.start.calls == 1


@pytest.mark.parametrize("control", [KeyboardInterrupt("carrier"), SystemExit("carrier"), OSError("carrier")])
def test_unknown_actual_delivery_keeps_entry_and_safe_reference(available, control):
    _, _, operation, access, main, workflow = available
    main.start.error = control
    with pytest.raises(type(control)) as caught:
        start(access)
    assert caught.value is control and control.__cause__.reference is not None
    (active,) = operation.active_inline_calls
    assert active.start is not None and active.operation.pending_remote_effects
    assert workflow.owner._active_borrow is active.borrow
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    with pytest.raises(StateError):
        operation.finish()
    assert main.start.calls == 1


def test_completed_resource_starts_exceed_history_cap_in_one_owner(available):
    database, _, operation, access, main, workflow = available
    for _ in range(140):
        start(access)
        assert workflow.owner.list_pending_lifecycle_obligations() == ()
    count = database._conn.execute(
        "SELECT COUNT(*) FROM lifecycle_obligations WHERE operation_id = ? AND obligation_kind = 'managed-start'",
        (workflow.owner.ownership.operation_id,),
    ).fetchone()[0]
    assert count == main.start.calls == 140 and operation.managed_runs == ()


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.windows
def test_unused_wrapper_setup_and_secondary_close_keep_original_control(available, closed):
    _, _, operation, access, main, workflow = available
    control = MemoryError("wrapper")
    original_cause = OSError("old")
    control.__cause__ = original_cause
    original = OperationBorrow.close
    seen = []

    def close(borrow):
        seen.append(borrow)
        if closed:
            original(borrow)
        raise SystemExit("close")

    with (
        patch.object(operation_module, "BorrowedFixedHelperCarrier", side_effect=control),
        patch.object(OperationBorrow, "close", close),
        pytest.raises(MemoryError) as caught,
    ):
        start(access)
    assert caught.value is control and control.__cause__.__cause__ is original_cause
    assert main.start.calls == 0 and operation.active_inline_calls == ()
    if closed:
        assert workflow.owner._active_borrow is None
    else:
        assert workflow.owner._active_borrow is seen[0]
        original(seen[0])
