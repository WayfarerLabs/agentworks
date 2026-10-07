"""Serial managed and file custody consume current debt, not receipt history."""

from __future__ import annotations

import pytest

from agentworks.db import LifecycleObligationState
from agentworks.errors import StateError
from agentworks.execution._execution_operation import InlineExecutionControlFact
from agentworks.execution._file_operation import FileOperation
from agentworks.execution.carrier import CarrierReport, Deadline, Dispatch
from agentworks.execution.models import Command, Lifetime, Output
from agentworks.execution.profiles import Protection
from tests.execution.files._target_support import target_for_owner
from tests.execution.test_execution_operation import RecordingCarrier
from tests.execution.test_managed_foreground_access import _status
from tests.execution.test_managed_foreground_access import bound as bound
from tests.execution.test_managed_foreground_access import framed_stream_reads as framed_stream_reads
from tests.execution.test_managed_foreground_access import view as view
from tests.execution.test_managed_lease_exchange import PLAN, RUNTIME

pytestmark = pytest.mark.windows


def test_300_completed_managed_runs_keep_only_current_debt(view, framed_stream_reads, monkeypatch):
    database, workflow, access, main, keeper = view
    _status(monkeypatch, 0)
    retained = []
    for _ in range(300):
        result = access.run(Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.discard())
        assert result.ok and result.job is not None
        run = workflow.views.execution_operation.managed_runs[-1]
        assert access.dispose(result.job).disposed is True
        run.finish_cleanup(Deadline.after(5))
        retained.append(run.disposal_obligation_id)
        (pending,) = workflow.owner.list_pending_lifecycle_obligations()
        assert pending.obligation_kind == "carrier-dispatch"
    assert main.start.calls == main.dispose.calls == keeper.clock.calls == 300
    assert main.observe.calls == 1200 and keeper.stop.calls == keeper.observe.calls == 0
    for identifier in (retained[0], retained[-1]):
        receipt = workflow.owner.inspect_lifecycle_obligation(identifier)
        assert receipt is not None and receipt.state is LifecycleObligationState.RESOLVED
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert database.operations.inspect(workflow.owner.ownership.scope) is None


def test_300_clean_file_admissions_preserve_receipts_without_pending_debt(view):
    database, workflow, _, _, _ = view
    files = FileOperation(workflow.owner, target_for_owner(workflow.owner))
    carrier = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    for _ in range(300):
        files.stat(
            carrier,
            trusted_root_path="/tmp",
            relative_path="capacity-owned",
            plan=PLAN,
            deadline=Deadline.after(5),
            runtime_selection=RUNTIME,
        )
        assert workflow.owner.list_pending_lifecycle_obligations() == ()
    assert carrier.calls == 300 and files.active_stats == ()
    assert (
        database._conn.execute(
            "SELECT COUNT(*) FROM lifecycle_obligations WHERE operation_id = ?",
            (workflow.owner.ownership.operation_id,),
        ).fetchone()[0]
        == 300
    )
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_existing_keeper_and_shared_reads_survive_other_admission_at_capacity(view, framed_stream_reads, monkeypatch):
    _, workflow, access, main, _ = view
    _status(monkeypatch, 0)
    job = access.start(
        Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION, output=Output.discard()
    )
    run = workflow.views.execution_operation.managed_runs[0]
    access.observe(job)
    debts = [
        workflow.owner.register_lifecycle_obligation("concurrent-work", payload_version=1, payload=b"retained")
        for _ in range(126)
    ]
    assert len(workflow.owner.list_pending_lifecycle_obligations()) == 128
    access.observe(job)
    with pytest.raises(StateError):
        access.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
    assert main.start.calls == 1 and not run.keeper._stop.is_set()
    assert len(workflow.owner.list_pending_lifecycle_obligations()) == 128
    debts.pop().resolve()
    assert access.dispose(job).disposed is True
    run.finish_cleanup(Deadline.after(5))
    for debt in debts:
        debt.resolve()
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("pending_count", [127, 128])
def test_real_pending_cap_managed_refusal_retains_no_phantom_keeper_or_start(view, pending_count):
    database, workflow, access, main, keeper = view
    obligations = [
        workflow.owner.register_lifecycle_obligation("concurrent-work", payload_version=1, payload=b"retained")
        for _ in range(pending_count)
    ]
    with pytest.raises(StateError):
        access.run(Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.discard())
    (run,) = workflow.views.execution_operation.managed_runs
    assert not run.keeper.admission_uncertain and not run.reservation_uncertain and not run.acknowledged
    assert main.start.calls == main.observe.calls == 0
    assert keeper.clock.calls == (1 if pending_count == 127 else 0)
    assert run.keeper.drain(Deadline.after(5)).drained
    run.finish_cleanup(Deadline.after(5))
    assert len(workflow.owner.list_pending_lifecycle_obligations()) == pending_count
    for obligation in obligations:
        obligation.resolve()
    workflow.close(cleanup_deadline=Deadline.after(5))
    assert database.operations.inspect(workflow.owner.ownership.scope) is None


def test_real_pending_cap_file_refusal_releases_never_dispatched_borrow(view):
    _, workflow, _, _, _ = view
    obligations = [
        workflow.owner.register_lifecycle_obligation("concurrent-work", payload_version=1, payload=b"retained")
        for _ in range(128)
    ]
    files = FileOperation(workflow.owner, target_for_owner(workflow.owner))
    carrier = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    with pytest.raises(StateError):
        files.stat(
            carrier,
            trusted_root_path="/tmp",
            relative_path="capacity-owned",
            plan=PLAN,
            deadline=Deadline.after(5),
            runtime_selection=RUNTIME,
        )
    assert carrier.calls == 0 and files.active_stats == ()
    workflow.owner.borrow().close()
    for obligation in obligations:
        obligation.resolve()
    workflow.close(cleanup_deadline=Deadline.after(5))


def test_true_pending_cap_direct_refusal_releases_its_unused_helper_custody(view):
    _, workflow, _, _, _ = view
    obligations = [
        workflow.owner.register_lifecycle_obligation("concurrent-work", payload_version=1, payload=b"retained")
        for _ in range(128)
    ]
    carrier = RecordingCarrier(CarrierReport(Dispatch.NOT_SENT))
    operation = workflow.views.execution_operation
    with pytest.raises(StateError) as caught:
        operation.run_inline(
            carrier, Command(["/bin/true"]), plan=PLAN, deadline=Deadline.after(5), runtime_selection=RUNTIME
        )
    assert isinstance(caught.value.__cause__, InlineExecutionControlFact)
    assert not caught.value.__cause__.outcome.requires_owner_retention
    assert carrier.calls == 0 and operation.active_inline_calls == ()
    workflow.owner.borrow().close()
    for obligation in obligations:
        obligation.resolve()
    workflow.close(cleanup_deadline=Deadline.after(5))


@pytest.mark.parametrize("failure", ["committed", "mismatched", "unreadable", "stale", "control"])
def test_keeper_registration_unknown_or_present_custody_is_not_erased(view, monkeypatch, failure):
    database, workflow, access, main, keeper = view
    original = type(database.operations).register_lifecycle_obligation
    control = KeyboardInterrupt() if failure == "control" else RuntimeError()

    def interrupted(repository, ownership, kind, version, payload, *, obligation_id=None):
        if failure in {"committed", "mismatched"}:
            original(
                repository,
                ownership,
                kind,
                version,
                b"mismatch" if failure == "mismatched" else payload,
                obligation_id=obligation_id,
            )
        elif failure == "stale":
            repository.recover_takeover(ownership, "d" * 32)
        raise control

    with monkeypatch.context() as patch:
        patch.setattr(type(database.operations), "register_lifecycle_obligation", interrupted)
        if failure == "unreadable":

            def unreadable(*args):
                raise OSError()

            patch.setattr(type(database.operations), "inspect_lifecycle_obligation", unreadable)
        with pytest.raises(BaseException) as caught:
            access.run(Command(["/bin/true"]), profile=Protection.MANAGED, output=Output.discard())
    assert caught.value is control
    (run,) = workflow.views.execution_operation.managed_runs
    assert run.keeper.admission_uncertain and run.keeper.obligation is None
    assert main.start.calls == main.observe.calls == keeper.clock.calls == 0
    assert run.keeper.drain(Deadline.after(5)).drained
    with pytest.raises(StateError):
        run.finish_cleanup(Deadline.after(5))
