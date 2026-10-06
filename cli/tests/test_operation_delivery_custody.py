"""Real local-worker custody under ordinary, recovery and aggregate ownership."""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._process import LocalProcessInput, LocalProcessRequest
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
)
from agentworks.operations import OperationOwner
from agentworks.vms._native_operation import _Workflow

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.operations import OperationAttempt, OperationBorrow, RecoveryAttempt, RecoveryDispatch

pytestmark = pytest.mark.windows


def _scope() -> OperationScope:
    return OperationScope(OperationResourceKind.VM, "delivery-custody-vm")


@pytest.fixture
def delayed_constructor(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[threading.Event, threading.Event]]:
    entered, release = threading.Event(), threading.Event()
    create = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(30)
        child = create(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", delayed)
    try:
        yield entered, release
    finally:
        release.set()
        assert all(child.returncode is not None for child in children)


def _launch(custody: LocalDeliveryCustody, entered: threading.Event) -> None:
    process = custody.begin_process()
    process.start(
        LocalProcessRequest(
            (sys.executable, "-I", "-c", "import time; time.sleep(60)"),
            LocalProcessInput.EOF,
        )
    )
    assert entered.wait(30)


def _attempt(
    db: Database, recovery: bool
) -> tuple[OperationOwner, OperationBorrow | RecoveryDispatch, OperationAttempt | RecoveryAttempt]:
    owner = OperationOwner.acquire(db.operations, _scope(), "test-local-delivery")
    if not recovery:
        borrow = owner.borrow()
        return owner, borrow, borrow.begin_attempt()
    owner = OperationOwner.recover(db.operations, owner.ownership, "b" * 32)
    obligation = owner.admit_recovery_support_obligation(
        "test-support", payload_version=1, payload=b"", obligation_id="c" * 32
    )
    retained = owner.rebind_possible_effect_lifecycle_obligation(
        obligation.obligation_id,
        "test-support",
        payload_version=1,
        payload=b"",
        payload_revision=obligation.payload_revision,
    )
    dispatch = retained.open_dispatch()
    return owner, dispatch, dispatch.begin_attempt()


@pytest.mark.parametrize("recovery", [False, True])
def test_handed_off_attempt_keeps_local_cleanup_separate_from_remote_debt(
    db: Database,
    delayed_constructor: tuple[threading.Event, threading.Event],
    recovery: bool,
) -> None:
    entered, release = delayed_constructor
    owner, dispatch, attempt = _attempt(db, recovery)
    try:
        _launch(attempt.local_delivery, entered)
        before = owner.list_lifecycle_obligations()
        with pytest.raises(StateError):
            attempt.settle()
        dispatch.handoff_unresolved()
        assert not owner.close_local_delivery(Deadline.after(0))
        with pytest.raises(StateError):
            owner.borrow()
        with pytest.raises(StateError):
            owner.close()
        assert db.operations.inspect(_scope()) is not None
        assert owner.list_lifecycle_obligations() == before
    finally:
        release.set()
        assert owner.close_local_delivery(Deadline.after(30))
    assert attempt.local_delivery.settled
    assert owner.list_lifecycle_obligations() == before
    with pytest.raises(StateError):
        attempt.settle()
    with pytest.raises(StateError):
        owner.close()
    assert db.operations.inspect(_scope()) is not None


def test_pre_target_workflow_releases_only_after_retained_worker_settles(
    db: Database, delayed_constructor: tuple[threading.Event, threading.Event]
) -> None:
    entered, release = delayed_constructor
    owner = OperationOwner.acquire(db.operations, _scope(), "test-pre-target-delivery")
    workflow = _Workflow(owner, Deadline.after(30))
    try:
        _launch(workflow.local_delivery, entered)
        with pytest.raises(StateError):
            workflow.close(cleanup_deadline=Deadline.after(0))
        assert db.operations.inspect(_scope()) is not None
        assert owner.list_lifecycle_obligations() == ()
        with pytest.raises(StateError):
            owner.borrow()
    finally:
        release.set()
        workflow.close(cleanup_deadline=Deadline.after(30))
    assert workflow.local_delivery.settled
    assert db.operations.inspect(_scope()) is None


@pytest.mark.parametrize(
    ("dispatch", "completion", "remote_pending"),
    [
        (Dispatch.NOT_SENT, None, False),
        (Dispatch.SENT, ExitStatus(code=0), False),
        (Dispatch.SENT, None, True),
        (Dispatch.UNKNOWN, None, True),
    ],
)
def test_remote_uncertainty_is_recorded_independently_of_pending_local_cleanup(
    db: Database, dispatch: Dispatch, completion: ExitStatus | None, remote_pending: bool
) -> None:
    class PendingCarrier:
        features = ChannelFeatures()

        @staticmethod
        def validate(invocation: PreparedInvocation, *, io: CarrierIO) -> None:
            pass

        @staticmethod
        def execute(
            invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody
        ) -> CarrierReport:
            custody.begin_process()
            return CarrierReport(dispatch, completion=completion)

    owner = OperationOwner.acquire(db.operations, _scope(), "test-separated-observations")
    helper = BorrowedFixedHelperCarrier(PendingCarrier(), owner.borrow())
    report = helper.execute(PreparedInvocation(("test-proof",)), io=CarrierIO(), deadline=Deadline.after(30))
    assert not helper.settle(report.dispatch, report.completion)
    assert helper.pending_remote_effects is remote_pending
    retained, error = helper.release()
    assert error is None and retained.requires_owner_retention
    assert owner.close_local_delivery(Deadline.after(30))
    with pytest.raises(StateError):
        owner.close()
