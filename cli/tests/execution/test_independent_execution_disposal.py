"""RESOURCE disposal retains actual helper custody and exact receipt-bound retry."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any
from unittest.mock import Mock, patch
from uuid import UUID

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _execution_operation as execution_module
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._managed_disposal_access import encode_managed_disposal_obligation
from agentworks.execution._managed_job_store import FactName
from agentworks.execution._managed_observation_protocol import ControllerState
from agentworks.execution._managed_runs import (
    ManagedLaunchObservation,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
)
from agentworks.execution._runtime_prerequisite import _NumericGuestBootstrap
from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation
from agentworks.execution.carrier import Deadline, Dispatch
from agentworks.execution.models import Command, JobRef, Lifetime, Output
from agentworks.execution.profiles import Protection
from agentworks.execution.result import ExecutionFailure
from agentworks.operations import OperationBorrow

from . import test_independent_execution_reads as reads
from . import test_managed_disposal as disposal
from .test_managed_job_access import RUN
from .test_managed_lease_exchange import PLAN, RUNTIME
from .test_managed_start_operation import GUEST

observer = reads.observer


def test_terminal_resource_disposal_resolves_exact_action_without_adoption(observer):
    database, repository, operation, access, main, workflow = observer
    before = repository.inspect(RUN)
    result = access.dispose(JobRef(RUN.run_id))
    assert result.disposed is True and result.failure is None
    assert main.observe.calls == main.dispose.calls == 1
    assert main.stop.calls == main.start.calls == main.clock.calls == 0
    assert operation.managed_runs == () and not operation.active_inline_calls
    rows = database.operations._connection.execute(
        "SELECT obligation_kind, state, payload FROM lifecycle_obligations WHERE operation_id = ?",
        (workflow.owner.ownership.operation_id,),
    ).fetchall()
    assert any(
        tuple(row) == ("managed-dispose", "resolved", encode_managed_disposal_obligation(RUN.run_id)) for row in rows
    )
    assert len(workflow.owner.list_pending_lifecycle_obligations()) == 1
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert repository.inspect(RUN) == before and main.dispose.calls == 1


@pytest.mark.parametrize("controller", (ControllerState.RUNNING, ControllerState.UNKNOWN))
def test_unproved_resource_disposal_only_observes(observer, controller):
    _, _, operation, access, main, _ = observer
    main.controller = controller
    assert access.dispose(JobRef(RUN.run_id)).disposed is False
    assert main.observe.calls == 1 and main.dispose.calls == main.stop.calls == 0
    assert operation.managed_runs == ()


@pytest.mark.parametrize(
    "missing", (FactName.LAUNCH, FactName.STDOUT_END, FactName.STDERR_END, FactName.BOUNDARY_EMPTY)
)
def test_incomplete_terminal_proof_never_disposes(observer, missing):
    _, _, _, access, main, _ = observer
    main.facts = tuple(name for name in main.facts if name is not missing)
    assert access.dispose(JobRef(RUN.run_id)).disposed is False
    assert main.dispose.calls == main.stop.calls == 0


def test_terminal_without_application_wait_disposes(observer):
    _, _, _, access, main, _ = observer
    main.facts = tuple(name for name in main.facts if name is not FactName.WAIT)
    assert access.dispose(JobRef(RUN.run_id)).disposed is True


@pytest.mark.parametrize("namespace", (None, ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "foreign")))
def test_missing_or_foreign_namespace_refuses_before_borrow(observer, namespace):
    _, _, operation, access, main, workflow = observer
    operation._resource_owner = namespace
    with patch.object(workflow.owner, "borrow", wraps=workflow.owner.borrow) as borrow, pytest.raises(ValidationError):
        access.dispose(JobRef(RUN.run_id))
    borrow.assert_not_called()
    assert main.observe.calls == main.dispose.calls == 0


@pytest.mark.parametrize("possible", (False, True))
def test_unconfirmed_resource_refuses_before_effects(observer, possible):
    _, repository, _, access, main, workflow = observer
    original = repository.inspect(RUN)
    record = repository.reserve(
        original.spec, identity=ManagedRunIdentity("f" * 32), output_policy=original.output_policy
    )
    if possible:
        repository.mark_possible_dispatch(record)
    with patch.object(workflow.owner, "borrow", wraps=workflow.owner.borrow) as borrow, pytest.raises(ValidationError):
        access.dispose(JobRef(record.identity.run_id))
    borrow.assert_not_called()
    assert main.observe.calls == main.dispose.calls == 0


def lose_response(observer: Any) -> Any:
    _, _, operation, access, main, _ = observer
    main.dispose.response = lambda request: b"invalid"
    result = access.dispose(JobRef(RUN.run_id))
    assert result.disposed is None
    (active,) = operation.active_inline_calls
    assert active.disposal is not None and active.borrow._closed
    return active


@pytest.mark.windows
def test_lost_response_retains_exact_action_and_retry_without_deleted_launch_read(observer):
    _, _, operation, access, main, workflow = observer
    active = lose_response(observer)
    old_id = active.disposal.obligation_id
    row = workflow.owner.inspect_lifecycle_obligation(old_id)
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    for callback in (operation.retry_inline_bookkeeping, operation.finish):
        with pytest.raises(StateError):
            callback()
    assert main.dispose.calls == main.observe.calls == 1
    # finish is permanently closed; use a separate test for explicit retry below.


def test_explicit_retry_reuses_unresolved_receipt_without_reobserving_deleted_facts(observer):
    _, _, operation, access, main, workflow = observer
    active = lose_response(observer)
    old_id = active.disposal.obligation_id
    main.facts = ()
    main.dispose.response = disposal._disposed
    assert access.dispose(JobRef(RUN.run_id)).disposed is True
    assert workflow.owner.inspect_lifecycle_obligation(old_id).state is LifecycleObligationState.RESOLVED
    assert main.observe.calls == 1 and main.dispose.calls == 2
    assert not operation.active_inline_calls


@pytest.mark.parametrize("retry_dispatch", (Dispatch.NOT_SENT, Dispatch.UNKNOWN))
def test_retry_delivery_cannot_erase_prior_remote_action_uncertainty(observer, retry_dispatch):
    _, _, operation, access, main, workflow = observer
    active = lose_response(observer)
    old_id = active.disposal.obligation_id
    key = active.tracking_key
    main.dispose.dispatch, main.dispose.code = retry_dispatch, None
    assert access.dispose(JobRef(RUN.run_id)).disposed is None
    (replacement,) = operation.active_inline_calls
    assert replacement is not active and replacement.tracking_key is key
    assert replacement.disposal.prior_remote_action_uncertain
    assert workflow.owner.inspect_lifecycle_obligation(old_id).state is LifecycleObligationState.POSSIBLE_EFFECT
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    assert main.dispose.calls == 2 and main.observe.calls == 1


def test_resolved_old_row_is_not_disposed_proof_and_uses_fresh_id(observer):
    _, _, operation, access, main, workflow = observer
    active = lose_response(observer)
    old_id = active.disposal.obligation_id
    workflow.owner._repository.resolve_lifecycle_obligation(workflow.owner.ownership, old_id)
    main.dispose.response = disposal._disposed
    with patch.object(
        OperationBorrow,
        "install_dispatch_obligation",
        autospec=True,
        side_effect=OperationBorrow.install_dispatch_obligation,
    ) as install:
        assert access.dispose(JobRef(RUN.run_id)).disposed is True
    new_id = install.call_args.args[1]
    assert new_id != old_id
    assert workflow.owner.inspect_lifecycle_obligation(new_id).state is LifecycleObligationState.RESOLVED
    assert main.observe.calls == 1 and main.dispose.calls == 2
    assert workflow.owner.inspect_lifecycle_obligation(old_id).state is LifecycleObligationState.RESOLVED


@pytest.mark.parametrize("where", ("install", "arm", "begin", "settle", "close"))
@pytest.mark.parametrize("after", (False, True))
@pytest.mark.windows
def test_original_controls_and_exact_bookkeeping_retry_never_dispatch(observer, where, after):
    _, _, operation, access, main, _ = observer
    method = {
        "install": "install_dispatch_obligation",
        "arm": "arm_dispatch_obligation",
        "begin": "begin_attempt",
        "settle": "settle",
        "close": "close",
    }[where]
    target = OperationBorrow if where != "settle" else BorrowedFixedHelperCarrier
    original = getattr(target, method)
    control = KeyboardInterrupt("original")
    cause = OSError("earlier")
    control.__cause__ = cause
    injected = False

    def fail_once(current, *args, **kwargs):
        nonlocal injected
        is_disposal = (
            (where == "settle" and main.dispose.calls > 0)
            or (
                where != "settle"
                and current._supplied_dispatch is not None
                and current._supplied_dispatch[1] == "managed-dispose"
            )
            or (where == "install" and len(args) > 1 and args[1] == "managed-dispose")
        )
        if not injected and is_disposal:
            injected = True
            if after:
                original(current, *args, **kwargs)
            raise control
        return original(current, *args, **kwargs)

    with patch.object(target, method, fail_once), pytest.raises(KeyboardInterrupt) as caught:
        access.dispose(JobRef(RUN.run_id))
    assert caught.value is control
    reference_fact = control.__cause__
    assert reference_fact is not None
    custody_fact = reference_fact.__cause__
    assert custody_fact is not None and custody_fact.__cause__ is cause
    calls = main.dispose.calls
    operation.retry_inline_bookkeeping()
    assert main.dispose.calls == calls and not operation.active_inline_calls


def test_active_ordinary_borrow_refuses_disposal_before_observation(observer):
    _, _, _, access, main, workflow = observer
    borrow = workflow.owner.borrow()
    with pytest.raises(StateError):
        access.dispose(JobRef(RUN.run_id))
    borrow.close()
    assert main.observe.calls == main.dispose.calls == 0


@pytest.mark.parametrize("outstanding", (False, True))
@pytest.mark.windows
def test_file_borrow_overlap_preserves_exact_existing_custody(observer, outstanding):
    _, _, _, access, main, workflow = observer
    borrow = workflow.views.file_operation._owner.borrow()
    attempt = borrow.begin_attempt() if outstanding else None
    try:
        with pytest.raises(StateError):
            access.dispose(JobRef(RUN.run_id))
        assert workflow.owner._active_borrow is borrow
        assert main.observe.calls == main.dispose.calls == 0
    finally:
        if attempt is not None:
            attempt.settle()
        borrow.close()


def test_planned_op_identity_never_falls_back_to_resource_disposal(observer, monkeypatch):
    _, repository, operation, access, main, _ = observer
    identifiers = iter((UUID(RUN.run_id), UUID("b" * 32), UUID("c" * 32)))
    monkeypatch.setattr(execution_module, "uuid4", lambda: next(identifiers))
    with pytest.raises(StateError):
        access.start(
            Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
        )
    assert operation.managed_runs
    with patch.object(repository, "inspect", wraps=repository.inspect) as inspect, pytest.raises(ValidationError):
        access.dispose(JobRef(RUN.run_id))
    inspect.assert_not_called()
    assert main.dispose.calls == main.observe.calls == 0


def test_unknown_terminal_observer_helper_prevents_disposal(observer):
    _, _, operation, access, main, _ = observer
    main.observe.dispatch, main.observe.code = Dispatch.UNKNOWN, None
    assert access.dispose(JobRef(RUN.run_id)).disposed is None
    assert main.dispose.calls == 0 and operation.unfinished_inline_executions


def test_unknown_disposal_helper_blocks_work_and_finish_without_replay(observer):
    _, _, operation, access, main, _ = observer
    main.dispose.dispatch, main.dispose.code = Dispatch.UNKNOWN, None
    assert access.dispose(JobRef(RUN.run_id)).disposed is None
    (active,) = operation.active_inline_calls
    for callback in (
        lambda: access.observe(JobRef(RUN.run_id)),
        lambda: access.stop(JobRef(RUN.run_id)),
        lambda: access.dispose(JobRef(RUN.run_id)),
        operation.retry_inline_bookkeeping,
        operation.finish,
    ):
        with pytest.raises(StateError):
            callback()
    assert operation.active_inline_calls == (active,) and main.dispose.calls == main.observe.calls == 1


@pytest.mark.parametrize("dispatch", (Dispatch.NOT_SENT, Dispatch.SENT))
def test_clean_no_effect_or_not_ready_action_can_be_explicitly_retried(observer, dispatch):
    _, _, operation, access, main, _ = observer
    from .test_managed_disposal_access import _not_ready

    main.dispose.dispatch = dispatch
    main.dispose.code = None if dispatch is Dispatch.NOT_SENT else 0
    main.dispose.response = _not_ready
    assert access.dispose(JobRef(RUN.run_id)).disposed is False
    assert not operation.active_inline_calls
    main.dispose.dispatch, main.dispose.code, main.dispose.response = Dispatch.SENT, 0, disposal._disposed
    assert access.dispose(JobRef(RUN.run_id)).disposed is True
    assert main.observe.calls == main.dispose.calls == 2


@pytest.mark.parametrize("after", (False, True))
@pytest.mark.windows
def test_atomic_replacement_publication_keeps_actual_entry_and_original_control(observer, after):
    _, _, operation, access, main, workflow = observer
    old = lose_response(observer)
    original_id = old.disposal.obligation_id
    control = KeyboardInterrupt("publication")
    original = operation._active_inline_calls

    class InterruptedPublication(dict):
        def __setitem__(self, key, value):
            if after:
                super().__setitem__(key, value)
            raise control

    operation._active_inline_calls = InterruptedPublication(original)
    with pytest.raises(KeyboardInterrupt) as caught:
        access.dispose(JobRef(RUN.run_id))
    assert caught.value is control and workflow.owner._active_borrow is None
    (retained,) = operation.active_inline_calls
    assert (retained is old) is (not after)
    assert retained.tracking_key is old.tracking_key
    assert workflow.owner.inspect_lifecycle_obligation(original_id).state is LifecycleObligationState.POSSIBLE_EFFECT
    operation._active_inline_calls = dict(operation._active_inline_calls)
    main.dispose.response = disposal._disposed
    main.facts = ()
    assert access.dispose(JobRef(RUN.run_id)).disposed is True
    assert main.dispose.calls == 2 and main.observe.calls == 1


def test_failed_retry_before_underlying_dispatch_does_not_resolve_prior_action(observer):
    _, _, operation, access, main, workflow = observer
    old = lose_response(observer)
    original_id = old.disposal.obligation_id
    main.dispose.refuse_validation = True
    with pytest.raises(ValidationError):
        access.dispose(JobRef(RUN.run_id))
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    assert workflow.owner.inspect_lifecycle_obligation(original_id).state is LifecycleObligationState.POSSIBLE_EFFECT
    main.dispose.refuse_validation = False
    main.dispose.response, main.facts = disposal._disposed, ()
    assert access.dispose(JobRef(RUN.run_id)).disposed is True
    assert main.dispose.calls == 2 and main.observe.calls == 1


@pytest.mark.parametrize("mutation", ("missing", "conflicting"))
def test_exact_pending_receipt_refuses_missing_or_conflicting_row(observer, mutation):
    database, _, operation, access, main, workflow = observer
    old = lose_response(observer)
    identity = old.disposal.obligation_id
    connection = database.operations._connection
    if mutation == "missing":
        connection.execute(
            "DELETE FROM lifecycle_obligations WHERE operation_id = ? AND obligation_id = ?",
            (workflow.owner.ownership.operation_id, identity),
        )
    else:
        connection.execute(
            "UPDATE lifecycle_obligations SET payload = ? WHERE operation_id = ? AND obligation_id = ?",
            (b"conflicting", workflow.owner.ownership.operation_id, identity),
        )
    with pytest.raises(StateError):
        access.dispose(JobRef(RUN.run_id))
    assert operation.active_inline_calls == (old,) and main.dispose.calls == 1


def test_route_changes_after_positive_observation_prevent_actual_disposal(observer):
    _, _, operation, access, main, _ = observer
    route = Mock(spec=WSL2OwnedOperation)
    control = StateError("selected route changed")

    def fence(deadline):
        if main.observe.calls:
            raise control

    route.require_selected_route.side_effect = fence
    operation._wsl2_route = route
    with pytest.raises(StateError) as caught:
        access.dispose(JobRef(RUN.run_id))
    assert caught.value is control and main.dispose.calls == 0 and main.observe.calls == 1
    operation.retry_inline_bookkeeping()
    assert not operation.active_inline_calls


@pytest.mark.parametrize("takeover", (False, True))
@pytest.mark.windows
def test_owner_closing_or_takeover_after_terminal_observation_prevents_delivery(observer, takeover):
    database, _, operation, access, main, workflow = observer
    observe = operation.observe_job

    def terminal_then_change_owner(reference, carrier, deadline):
        result = observe(reference, carrier, deadline)
        if takeover:
            database.operations.recover_takeover(workflow.owner.ownership, "b" * 32)
        else:
            with pytest.raises(StateError):
                workflow.owner.close()
        return result

    operation.observe_job = terminal_then_change_owner
    with pytest.raises(StateError):
        access.dispose(JobRef(RUN.run_id))
    assert main.observe.calls == 1 and main.dispose.calls == 0


@pytest.mark.parametrize("change", ("vm", "marker", "boot", "runtime", "repository"))
def test_exact_context_mismatch_refuses_disposal_before_borrow(observer, change):
    _, _, operation, access, main, workflow = observer
    guest = GUEST
    if change == "vm":
        operation._target = replace(operation._target, name="another-vm")
    elif change in {"marker", "boot"}:
        guest = replace(
            GUEST,
            **{
                "instance_marker" if change == "marker" else "boot_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"
                if change == "boot"
                else "f" * 32
            },
        )
        operation._bootstrap = _NumericGuestBootstrap(PLAN, guest)
        if change == "marker":
            operation._resource_owner = ManagedRunOwner(ManagedRunOwnerKind.RESOURCE, "vm:" + guest.instance_marker)
    elif change == "runtime":
        operation._native_binding = replace(
            operation._native_binding, runtime_selection=replace(RUNTIME, explicit_path="/other/python")
        )
    else:
        operation._managed_repository = Mock()
    with (
        patch.object(workflow.owner, "borrow", wraps=workflow.owner.borrow) as borrow,
        pytest.raises((StateError, ValidationError)),
    ):
        access.dispose(JobRef(RUN.run_id))
    borrow.assert_not_called()
    assert main.observe.calls == main.dispose.calls == 0


@pytest.mark.parametrize("control", (KeyboardInterrupt("delivery"), SystemExit("delivery"), OSError("delivery")))
def test_escaping_native_control_preserves_identity_reference_and_unknown_actual_helper(observer, control):
    _, _, operation, access, main, _ = observer
    prior = OSError("old explicit cause")
    control.__cause__ = prior

    def fail(request):
        raise control

    main.dispose.response = fail
    with pytest.raises(type(control)) as caught:
        access.dispose(JobRef(RUN.run_id))
    assert caught.value is control and control.__cause__.reference == JobRef(RUN.run_id)
    assert control.__cause__.__cause__.__cause__ is prior
    (active,) = operation.active_inline_calls
    assert active.disposal is not None and active.operation.pending_remote_effects
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    assert main.dispose.calls == 1


@pytest.mark.parametrize("closed", (False, True))
@pytest.mark.windows
def test_retry_allocation_and_secondary_close_keep_primary_and_exact_original_receipt(observer, closed):
    _, _, operation, access, main, workflow = observer
    old = lose_response(observer)
    control = MemoryError("allocation")
    secondary = SystemExit("secondary close")
    original_close = OperationBorrow.close
    seen = []

    def fail_close(borrow):
        seen.append(borrow)
        if closed:
            original_close(borrow)
        raise secondary

    with (
        patch.object(execution_module, "BorrowedFixedHelperCarrier", side_effect=control),
        patch.object(OperationBorrow, "close", fail_close),
        pytest.raises(MemoryError) as caught,
    ):
        access.dispose(JobRef(RUN.run_id))
    assert caught.value is control and operation.active_inline_calls == (old,)
    assert len(seen) == 1
    if closed:
        assert workflow.owner._active_borrow is None
    else:
        assert workflow.owner._active_borrow is seen[0]
        original_close(seen[0])
    main.dispose.response = disposal._disposed
    assert access.dispose(JobRef(RUN.run_id)).disposed is True
    assert main.dispose.calls == 2 and main.observe.calls == 1


@pytest.mark.parametrize("after", (False, True))
@pytest.mark.windows
def test_lost_exact_action_resolution_reply_reconciles_without_carrier_replay(observer, after):
    _, _, operation, access, main, workflow = observer
    repository = workflow.owner._repository
    original = type(repository).resolve_lifecycle_obligation
    control = KeyboardInterrupt("resolution reply")
    injected = False

    def interrupted(current, ownership, identity):
        nonlocal injected
        row = current.inspect_lifecycle_obligation(ownership, identity)
        if not injected and row.obligation_kind == "managed-dispose":
            injected = True
            if after:
                original(current, ownership, identity)
            raise control
        return original(current, ownership, identity)

    with (
        patch.object(type(repository), "resolve_lifecycle_obligation", interrupted),
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        access.dispose(JobRef(RUN.run_id))
    assert caught.value is control
    operation.retry_inline_bookkeeping()
    assert not operation.active_inline_calls and main.observe.calls == main.dispose.calls == 1
    workflow.close(cleanup_deadline=Deadline.after(2))
    assert main.observe.calls == main.dispose.calls == 1


@pytest.mark.parametrize("after", (False, True))
@pytest.mark.windows
def test_lost_unknown_action_handoff_reply_keeps_receipt_for_explicit_retry(observer, after):
    _, _, operation, access, main, _ = observer
    original = OperationBorrow.handoff_retained_effect
    control = SystemExit("handoff reply")
    injected = False

    def interrupted(borrow):
        nonlocal injected
        if not injected and borrow._supplied_dispatch[1] == "managed-dispose":
            injected = True
            if after:
                original(borrow)
            raise control
        return original(borrow)

    main.dispose.response = lambda request: b"invalid"
    with patch.object(OperationBorrow, "handoff_retained_effect", interrupted), pytest.raises(SystemExit) as caught:
        access.dispose(JobRef(RUN.run_id))
    assert caught.value is control and operation.active_inline_calls
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    assert main.dispose.calls == main.observe.calls == 1
    main.dispose.response, main.facts = disposal._disposed, ()
    assert access.dispose(JobRef(RUN.run_id)).disposed is True
    assert main.observe.calls == 1 and main.dispose.calls == 2


@pytest.mark.parametrize("prior_op", (False, True))
def test_pending_disposal_cannot_adopt_another_reference_or_prior_op(observer, prior_op):
    _, repository, operation, access, main, workflow = observer
    old = lose_response(observer)
    spec = repository.inspect(RUN).spec
    if prior_op:
        spec = replace(
            spec, owner=ManagedRunOwner(ManagedRunOwnerKind.OPERATION, "a" * 32), lifetime=ManagedRunLifetime.OPERATION
        )
    second = repository.reserve(
        spec, identity=ManagedRunIdentity("f" * 32), output_policy=repository.inspect(RUN).output_policy
    )
    second = repository.mark_possible_dispatch(second)
    repository.reconcile(
        second,
        ManagedLaunchObservation(Dispatch.SENT, ManagedRunReceipt(second.identity, second.identity.unit_name, spec)),
    )
    with (
        patch.object(workflow.owner, "borrow", wraps=workflow.owner.borrow) as borrow,
        pytest.raises((ValidationError, StateError)),
    ):
        access.dispose(JobRef(second.identity.run_id))
    borrow.assert_not_called()
    assert operation.active_inline_calls == (old,) and main.dispose.calls == main.observe.calls == 1


def test_resource_disposal_uses_one_original_finite_deadline(observer):
    _, _, _, access, main, _ = observer
    original = main.execute
    deadlines = []

    def execute(invocation, *, io, deadline, custody):
        deadlines.append(deadline)
        return original(invocation, io=io, deadline=deadline, custody=custody)

    main.execute = execute
    selected = Deadline.after(2)
    assert access.dispose(JobRef(RUN.run_id), deadline=selected).disposed is True
    assert len(deadlines) == 2 and all(deadline is selected for deadline in deadlines)


def test_terminal_observation_consuming_deadline_never_dispatches_disposal(observer, monkeypatch):
    _, _, operation, access, main, _ = observer
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    observe = operation.observe_job

    def late_observe(reference, carrier, deadline):
        outcome = observe(reference, carrier, deadline)
        clock[0] = 2.0
        return outcome

    operation.observe_job = late_observe
    result = access.dispose(JobRef(RUN.run_id), deadline=Deadline.after(1))
    assert result.disposed is False and result.deadline_exceeded and result.failure is ExecutionFailure.DEADLINE
    assert main.observe.calls == 1 and main.dispose.calls == 0 and not operation.active_inline_calls


@pytest.mark.parametrize("method", ("install_dispatch_obligation", "arm_dispatch_obligation", "begin_attempt"))
@pytest.mark.parametrize("after", (False, True))
@pytest.mark.windows
def test_retry_setup_control_cannot_resolve_prior_action_and_eventual_explicit_retry_works(observer, method, after):
    _, _, operation, access, main, workflow = observer
    old = lose_response(observer)
    identity = old.disposal.obligation_id
    original = getattr(OperationBorrow, method)
    control = KeyboardInterrupt("retry setup")
    injected = False

    def interrupted(borrow, *args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            if after:
                original(borrow, *args, **kwargs)
            raise control
        return original(borrow, *args, **kwargs)

    with patch.object(OperationBorrow, method, interrupted), pytest.raises(KeyboardInterrupt) as caught:
        access.dispose(JobRef(RUN.run_id))
    assert caught.value is control and main.dispose.calls == 1
    with pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    assert workflow.owner.inspect_lifecycle_obligation(identity).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert workflow.owner._active_borrow is None
    main.dispose.response, main.facts = disposal._disposed, ()
    assert access.dispose(JobRef(RUN.run_id)).disposed is True
    assert main.dispose.calls == 2 and main.observe.calls == 1 and not operation.active_inline_calls


def interrupt_retry_before_install(observer: Any) -> Any:
    _, _, _, access, _, _ = observer
    old = lose_response(observer)
    with (
        patch.object(OperationBorrow, "install_dispatch_obligation", side_effect=KeyboardInterrupt("before install")),
        pytest.raises(KeyboardInterrupt),
    ):
        access.dispose(JobRef(RUN.run_id))
    return old.disposal.obligation_id


@pytest.mark.parametrize("after", (False, True))
@pytest.mark.windows
def test_lost_idempotent_prior_action_install_reply_keeps_actual_unused_borrow(observer, after):
    _, _, operation, access, main, workflow = observer
    identity = interrupt_retry_before_install(observer)
    original = OperationBorrow.install_dispatch_obligation
    control = SystemExit("idempotent install reply")

    def interrupted(borrow, *args, **kwargs):
        if after:
            original(borrow, *args, **kwargs)
        raise control

    with patch.object(OperationBorrow, "install_dispatch_obligation", interrupted), pytest.raises(SystemExit) as caught:
        operation.retry_inline_bookkeeping()
    assert caught.value is control
    (active,) = operation.active_inline_calls
    assert workflow.owner._active_borrow is active.borrow
    assert workflow.owner.inspect_lifecycle_obligation(identity).state is LifecycleObligationState.POSSIBLE_EFFECT
    assert main.observe.calls == main.dispose.calls == 1
    main.dispose.response, main.facts = disposal._disposed, ()
    assert access.dispose(JobRef(RUN.run_id)).disposed is True
    assert main.observe.calls == 1 and main.dispose.calls == 2


@pytest.mark.parametrize("takeover", (False, True))
@pytest.mark.windows
def test_prior_action_binding_reinstall_rechecks_takeover_and_closing_before_local_handoff(observer, takeover):
    database, _, operation, _, main, workflow = observer
    identity = interrupt_retry_before_install(observer)
    original = OperationBorrow.install_dispatch_obligation

    def changed_owner(borrow, *args, **kwargs):
        if takeover:
            database.operations.recover_takeover(workflow.owner.ownership, "b" * 32)
        else:
            with pytest.raises(StateError):
                workflow.owner.close()
        return original(borrow, *args, **kwargs)

    with patch.object(OperationBorrow, "install_dispatch_obligation", changed_owner), pytest.raises(StateError):
        operation.retry_inline_bookkeeping()
    (active,) = operation.active_inline_calls
    assert active.disposal.obligation_id == identity and workflow.owner._active_borrow is active.borrow
    assert main.observe.calls == main.dispose.calls == 1
