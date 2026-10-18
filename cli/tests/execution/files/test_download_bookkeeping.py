"""Actual SQLite, helpers and workstation custody for ordinary downloads."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from agentworks.db import LifecycleObligation as PersistedLifecycleObligation
from agentworks.db import LifecycleObligationState
from agentworks.errors import ExternalError, StateError
from agentworks.execution import _file_local_download as local
from agentworks.execution import access as access_module
from agentworks.execution._file_download import FileDownloadStatus, _PreparedDownload
from agentworks.execution._file_local_download import FileLocalDownloadControlFact
from agentworks.execution._file_obligation import (
    FileCallObligation,
    decode_file_call_obligation,
)
from agentworks.execution._file_result_transfer import reduce_file_local_download
from agentworks.execution._local_download_publication import LocalDownloadPublication
from agentworks.execution.files import Create, Replace
from agentworks.operations import LifecycleObligation, OperationBorrow, OperationOwner, _is_pre_registration_refusal
from tests.execution._bound_carrier_support import obligation_receipt
from tests.execution.files._file_access_support import bound_access as bound_access
from tests.execution.files._file_access_support import plan as plan
from tests.execution.files._file_snapshot_support import LocalCarrier

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="actual workstation/helper proof requires Linux")


def pending(owner: OperationOwner) -> tuple[PersistedLifecycleObligation, FileCallObligation]:
    rows = owner.list_pending_lifecycle_obligations()
    assert len(rows) == 1
    return rows[0], decode_file_call_obligation(rows[0].payload)


@pytest.mark.parametrize(
    "fault", ["committed-error", "committed-control", "unreadable", "inspection-control", "absent"]
)
def test_registration_failure_preserves_original_custody_unless_absence_is_proved(bound_access, monkeypatch, fault):
    from agentworks.execution._execution_operation import ExecutionOperation
    from agentworks.execution.carrier import Deadline
    from agentworks.vms._native_operation import VMOperation, _Workflow
    from tests.execution.files._target_support import target_for_owner

    access, root, owner, db = bound_access
    source = root / "source"
    source.write_bytes(b"verified")
    destination = root.parent / "destination"
    carrier = LocalCarrier()
    monkeypatch.setattr(access, "_carrier", carrier)
    register = type(db.operations).register_lifecycle_obligation
    primary = KeyboardInterrupt() if fault == "committed-control" else OSError()
    prior_cause = RuntimeError()
    primary.__cause__ = prior_cause
    inspection_control = KeyboardInterrupt()
    admissions = []
    constructors = []
    failures = []

    def interrupted(repository, ownership, kind, version, payload, *, obligation_id=None):
        call = access._operation._local_download_call
        admissions.append((call, call.active, obligation_id, version, payload))
        if fault.startswith("committed"):
            register(repository, ownership, kind, version, payload, obligation_id=obligation_id)
        raise primary

    def inspect_failure(*args, **kwargs):
        if fault == "inspection-control":
            raise inspection_control
        raise OSError()

    def construct(*args, **kwargs):
        constructors.append(True)
        raise AssertionError("registration failure reached stage construction")

    def reduce(outcome, *, failure=None, **kwargs):
        failures.append(failure)
        return reduce_file_local_download(outcome, failure=failure, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(db.operations), "register_lifecycle_obligation", interrupted)
        if fault in {"unreadable", "inspection-control"}:
            patch.setattr(type(db.operations), "inspect_lifecycle_obligation", inspect_failure)
        patch.setattr(local, "_publisher_for_host", construct)
        patch.setattr(access_module, "reduce_file_local_download", reduce)
        with pytest.raises(KeyboardInterrupt if "control" in fault else ExternalError) as raised:
            access.download(PurePosixPath(source), destination)

    escaped = inspection_control if fault == "inspection-control" else primary
    fact = escaped.__cause__
    assert isinstance(fact, FileLocalDownloadControlFact)
    if "control" in fault:
        assert raised.value is escaped and failures == []
    else:
        assert failures == [primary]
    if fault == "inspection-control":
        assert inspection_control.__context__ is primary and primary.__cause__ is prior_cause
    elif fault == "absent":
        assert fact.__cause__ is not None
        assert _is_pre_registration_refusal(fact) and fact.__cause__.__cause__ is prior_cause
    else:
        assert fact.__cause__ is prior_cause
    assert constructors == [] and carrier.calls == 0 and not destination.exists()
    original_call, active, identifier, version, payload = admissions[0]
    record = decode_file_call_obligation(payload)
    assert record.download_child is None
    assert isinstance(active.prepared, _PreparedDownload) and active.prepared.state.token == record.token
    assert active.obligation_id == identifier and active.borrow is original_call.borrow
    assert active.obligation is None and original_call.borrow._dispatch_obligation is None
    rows = owner.list_pending_lifecycle_obligations()
    if fault.startswith("committed"):
        assert len(rows) == 1 and rows[0].obligation_id == identifier
        assert rows[0].state is LifecycleObligationState.REGISTERED
        assert rows[0].payload_version == version and rows[0].payload == payload
    else:
        assert rows == ()
    if fault == "absent":
        assert original_call.active is None and original_call.borrow.closed
        assert access._operation.active_downloads == () and access._operation._local_download_call is None
        assert access.download(PurePosixPath(source), destination).size == 8
        return
    assert access._operation._local_download_call is original_call and original_call.active is active
    assert access._operation.active_downloads == (active,) and not original_call.borrow.closed
    assert original_call.stage is None and not original_call.dispatched
    assert original_call.local_outcome is fact.outcome and fact.outcome.download is None
    assert original_call.reduction_complete
    with pytest.raises(StateError):
        access.download(PurePosixPath(source), root.parent / "next")
    with pytest.raises(StateError):
        access._operation.settle_download_bookkeeping()
    workflow = _Workflow(owner, Deadline.after(20))
    workflow.views = VMOperation(
        owner, None, None, access._operation, ExecutionOperation(owner, target_for_owner(owner)), None
    )
    with pytest.raises(StateError):
        workflow.close()
    assert access._operation._local_download_call is original_call
    assert access._operation.active_downloads == (active,) and not original_call.borrow.closed
    assert owner.list_pending_lifecycle_obligations() == rows
    assert db.operations.inspect(owner.ownership.scope) is not None
    assert constructors == [] and carrier.calls == 0 and not destination.exists()


@pytest.mark.parametrize("condition", [Create(), Replace()])
def test_original_admission_before_stage_and_helper_then_close_after_reduction(bound_access, monkeypatch, condition):
    access, root, owner, _db = bound_access
    (root / "source").write_bytes(b"verified")
    publisher = local._publisher_for_host
    reducer = reduce_file_local_download
    close = OperationBorrow.close
    events = []
    admitted = []

    def check_false() -> None:
        row, record = pending(owner)
        assert row.payload_version == 1 and row.payload_revision == 0
        assert record.download_child is None
        admitted.append((row.obligation_id, record.token))

    class InspectingCarrier(LocalCarrier):
        def execute(self, *args, **kwargs):
            check_false()
            return super().execute(*args, **kwargs)

    def construct(*args, **kwargs):
        check_false()
        events.append("construct")
        return publisher(*args, **kwargs)

    def reduce(outcome, **kwargs):
        check_false()
        call = access._operation._local_download_call
        assert call.active.outcome is outcome.download
        assert call.local_outcome is outcome
        assert call.stage.published and not call.stage.cleanup_uncertain
        assert not outcome.cleanup_failed and outcome.unfinished_stage is None
        result = reducer(outcome, **kwargs)
        events.append("reduce")
        return result

    def closing(borrow):
        row, record = pending(owner)
        assert record.download_child is None and row.payload_revision == 0
        assert record.token == admitted[0][1]
        assert events == ["construct", "reduce"]
        close(borrow)

    monkeypatch.setattr(local, "_publisher_for_host", construct)
    monkeypatch.setattr(access_module, "reduce_file_local_download", reduce)
    monkeypatch.setattr(OperationBorrow, "close", closing)
    carrier = InspectingCarrier()
    monkeypatch.setattr(access, "_carrier", carrier)
    destination = root.parent / "destination"
    if isinstance(condition, Replace):
        destination.write_bytes(b"original")
    assert access.download(PurePosixPath(root / "source"), destination, local_condition=condition).size == 8
    assert destination.read_bytes() == b"verified"
    assert len(set(admitted)) == 1
    row = obligation_receipt(owner, admitted[0][0])
    assert row.state is LifecycleObligationState.RESOLVED
    assert decode_file_call_obligation(row.payload).download_child is None and row.payload_revision == 0


@pytest.mark.parametrize(
    "fault", ["constructor", "commit", "abort", "reduction", "control", "absent", "capture", "allocation"]
)
def test_failed_calls_and_incomplete_capture_keep_truthful_custody(bound_access, monkeypatch, fault):
    access, root, owner, _db = bound_access
    source = root / "source"
    if fault != "absent":
        source.write_bytes(b"verified")
    carrier = LocalCarrier()
    monkeypatch.setattr(access, "_carrier", carrier)
    receipt = []
    publisher = local._publisher_for_host

    def construct(*args, **kwargs):
        row, record = pending(owner)
        receipt.append(row.obligation_id)
        assert record.download_child is None
        if fault == "constructor":
            raise FileExistsError()
        return publisher(*args, **kwargs)

    def fail(*args, **kwargs):
        raise KeyboardInterrupt() if fault == "control" else RuntimeError()

    monkeypatch.setattr(local, "_publisher_for_host", construct)
    if fault in {"commit", "control"}:
        monkeypatch.setattr(LocalDownloadPublication, "commit", fail)
    if fault == "abort":
        monkeypatch.setattr(LocalDownloadPublication, "abort", fail)
    if fault == "reduction":
        monkeypatch.setattr(access_module, "reduce_file_local_download", fail)
    if fault == "capture":
        monkeypatch.setattr(access._operation, "_capture", fail)
    if fault == "allocation":
        monkeypatch.setattr(local, "FileLocalDownloadOutcome", fail)
    with pytest.raises(KeyboardInterrupt if fault == "control" else Exception):
        access.download(PurePosixPath(source), root.parent / "destination")
    row = obligation_receipt(owner, receipt[0])
    assert not decode_file_call_obligation(row.payload).download_child_complete
    if fault in {"capture", "allocation"}:
        call = access._operation._local_download_call
        assert call is not None and call.active is not None
        assert call.active.obligation.obligation_id == receipt[0]
        assert call.stage is not None
        with pytest.raises(StateError):
            access._operation.settle_download_bookkeeping()
        with pytest.raises(StateError):
            access.download(PurePosixPath(source), root.parent / "next")
    else:
        assert row.state is LifecycleObligationState.RESOLVED
    if fault == "constructor":
        assert carrier.calls == 0


def test_aggregate_settles_captured_closure_after_stop_before_component_check(bound_access, monkeypatch):
    from agentworks.execution._execution_operation import ExecutionOperation
    from agentworks.execution.carrier import Deadline
    from agentworks.vms._native_operation import VMOperation, _Workflow
    from tests.execution.files._target_support import target_for_owner

    access, root, owner, db = bound_access
    (root / "source").write_bytes(b"verified")
    carrier = LocalCarrier()
    monkeypatch.setattr(access, "_carrier", carrier)
    close = OperationBorrow.close

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(OperationBorrow, "close", interrupt)
    with pytest.raises(KeyboardInterrupt):
        access.download(PurePosixPath(root / "source"), root.parent / "destination")
    workflow = _Workflow(owner, Deadline.after(20))
    workflow.views = VMOperation(
        owner,
        None,
        None,
        access._operation,
        ExecutionOperation(owner, target_for_owner(owner)),
        None,
    )
    with pytest.raises(KeyboardInterrupt):
        workflow.close()
    count = carrier.calls
    monkeypatch.setattr(OperationBorrow, "close", close)
    workflow.close(cleanup_deadline=Deadline.after(20))
    assert carrier.calls == count and access._operation._local_download_call is None
    assert db.operations.inspect(owner.ownership.scope) is None


@pytest.mark.parametrize(
    "fault",
    [
        "nonzero",
        "uncertain",
        "late-publication",
        "late-reduction",
        "published-control",
        "published-error",
        "cleanup-retry",
    ],
)
def test_actual_remote_and_late_local_failures_keep_original_outcome(bound_access, monkeypatch, fault):
    from agentworks.execution.carrier import Deadline, ExitStatus

    access, root, owner, _db = bound_access
    (root / "source").write_bytes(b"verified")
    commit = LocalDownloadPublication.commit
    abort = LocalDownloadPublication.abort
    enabled = True
    receipts = []
    calls = []
    begin = access._operation.begin_local_download

    def capture_call():
        call = begin()
        calls.append(call)
        return call

    monkeypatch.setattr(access._operation, "begin_local_download", capture_call)

    class FaultCarrier(LocalCarrier):
        def execute(self, *args, **kwargs):
            receipts.append(pending(owner)[0].obligation_id)
            report = super().execute(*args, **kwargs)
            if self.calls == 2:
                if fault == "nonzero":
                    return replace(report, completion=ExitStatus(code=1))
                if fault == "uncertain":
                    return replace(report, completion=None)
            return report

    carrier = FaultCarrier()
    monkeypatch.setattr(access, "_carrier", carrier)

    def changed(stage, **kwargs):
        commit(stage, **kwargs)
        if fault == "published-control":
            raise KeyboardInterrupt()
        if fault == "published-error":
            raise RuntimeError()
        if fault == "late-publication":
            monkeypatch.setattr(Deadline, "expired", property(lambda self: True))

    def clean(stage):
        if enabled and fault == "cleanup-retry":
            raise RuntimeError()
        abort(stage)

    monkeypatch.setattr(LocalDownloadPublication, "commit", changed)
    monkeypatch.setattr(LocalDownloadPublication, "abort", clean)
    if fault == "late-reduction":

        def expired_reduce(outcome, **kwargs):
            result = reduce_file_local_download(outcome, **kwargs)
            monkeypatch.setattr(Deadline, "expired", property(lambda self: True))
            return result

        monkeypatch.setattr(access_module, "reduce_file_local_download", expired_reduce)
    with pytest.raises(KeyboardInterrupt if fault == "published-control" else Exception):
        access.download(PurePosixPath(root / "source"), root.parent / "destination")
    original = obligation_receipt(owner, receipts[0])
    assert not decode_file_call_obligation(original.payload).download_child_complete
    assert calls[0].reduction_complete
    if fault == "uncertain":
        assert original.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert decode_file_call_obligation(original.payload).download_child is None
    if fault == "cleanup-retry":
        failed_outcome = calls[0].local_outcome
        enabled = False
        assert access.download(PurePosixPath(root / "source"), root.parent / "next").size == 8
        assert obligation_receipt(owner, receipts[0]).payload == original.payload
        assert calls[0].local_outcome is failed_outcome and failed_outcome.cleanup_failed
        assert calls[1].reduction_complete and not calls[1].local_outcome.cleanup_failed


@pytest.mark.parametrize("fault", ["close-control", "committed-control", "closed-control"])
def test_exact_closure_retry_retains_actual_local_result(bound_access, monkeypatch, fault):
    access, root, owner, db = bound_access
    (root / "source").write_bytes(b"verified")
    carrier = LocalCarrier()
    monkeypatch.setattr(access, "_carrier", carrier)
    close = OperationBorrow.close
    resolve = type(db.operations).resolve_lifecycle_obligation
    enabled = True
    receipts = []
    publications = []
    commit = LocalDownloadPublication.commit

    def publishing(stage, **kwargs):
        publications.append(stage)
        return commit(stage, **kwargs)

    def interrupted_close(borrow):
        receipts.append(borrow._dispatch_obligation.obligation_id)
        if enabled and fault == "close-control":
            raise KeyboardInterrupt()
        close(borrow)
        if enabled and fault == "closed-control":
            raise KeyboardInterrupt()

    def interrupted_resolve(repository, ownership, obligation_id):
        result = resolve(repository, ownership, obligation_id)
        if enabled and fault == "committed-control":
            raise KeyboardInterrupt()
        return result

    def forbidden(*args, **kwargs):
        raise AssertionError("successful ordinary download published history")

    monkeypatch.setattr(OperationBorrow, "close", interrupted_close)
    monkeypatch.setattr(LocalDownloadPublication, "commit", publishing)
    monkeypatch.setattr(type(db.operations), "resolve_lifecycle_obligation", interrupted_resolve)
    monkeypatch.setattr(LifecycleObligation, "publish_payload", forbidden)
    destination = root.parent / "destination"
    with pytest.raises(KeyboardInterrupt):
        access.download(PurePosixPath(root / "source"), destination)
    call = access._operation._local_download_call
    assert call is not None and call.active.outcome.status is FileDownloadStatus.COMPLETE
    assert call.local_outcome.published and call.reduction_complete
    assert call.local_outcome.download is call.active.outcome
    assert call.stage.published and not call.stage.cleanup_uncertain
    assert call.borrow.closed is (fault == "closed-control")
    with pytest.raises(StateError):
        access.download(PurePosixPath(root / "source"), root.parent / "next")
    count = carrier.calls
    enabled = False
    access._operation.settle_download_bookkeeping()
    access._operation.settle_download_bookkeeping()
    assert carrier.calls == count and destination.read_bytes() == b"verified"
    assert publications == [call.stage]
    assert access._operation._local_download_call is None
    row = obligation_receipt(owner, receipts[0])
    assert row.state is LifecycleObligationState.RESOLVED and row.payload_revision == 0
    with pytest.raises(StateError):
        owner.borrow()


@pytest.mark.parametrize("fault", ["lost", "unconfirmed", "control", "committed-control", "retention"])
def test_remote_debt_publication_retry_keeps_failed_local_outcome(bound_access, monkeypatch, fault):
    access, root, owner, db = bound_access
    (root / "source").write_bytes(b"verified")
    publish = LifecycleObligation.publish_payload
    retain = access._operation._retain_unfinished
    enabled = True
    intents = []

    class FaultCarrier(LocalCarrier):
        def execute(self, *args, **kwargs):
            report = super().execute(*args, **kwargs)
            if self.calls == 2:
                return replace(report, completion=None)
            return report

    carrier = FaultCarrier()
    monkeypatch.setattr(access, "_carrier", carrier)

    def interrupted(handle, *, expected_revision, payload_version, payload):
        intents.append((handle.obligation_id, expected_revision, payload_version, payload))
        if enabled and fault == "unconfirmed":
            raise RuntimeError()
        if enabled and fault == "control":
            raise KeyboardInterrupt()
        row = publish(handle, expected_revision=expected_revision, payload_version=payload_version, payload=payload)
        if enabled and fault == "lost" and len(intents) == 1:
            raise RuntimeError()
        if enabled and fault == "committed-control":
            raise KeyboardInterrupt()
        return row

    monkeypatch.setattr(LifecycleObligation, "publish_payload", interrupted)

    def interrupted_retention(unfinished):
        retain(unfinished)
        if enabled and fault == "retention":
            raise KeyboardInterrupt()

    monkeypatch.setattr(access._operation, "_retain_unfinished", interrupted_retention)
    destination = root.parent / "destination"
    with pytest.raises(KeyboardInterrupt if "control" in fault or fault == "retention" else Exception):
        access.download(PurePosixPath(root / "source"), destination)
    call = access._operation._local_download_call
    if fault == "lost":
        assert call is None and len(intents) == 2
    else:
        assert call is not None and call.reduction_complete
        assert not call.local_outcome.published
        active = call.active
        assert active.outcome.requires_owner_retention and active.retained_payload is not None
        captured = call.local_outcome
    count = carrier.calls
    enabled = False
    access._operation.settle_download_bookkeeping()
    access._operation.settle_download_bookkeeping()
    assert not access._operation.active_downloads and access._operation._local_download_call is None
    assert carrier.calls == count and not destination.exists()
    assert len(access._operation.unfinished_downloads) == 1
    if fault != "lost":
        assert call.local_outcome is captured and not captured.published
        assert access._operation.unfinished_downloads[0].outcome is active.outcome
    assert len(set(intents)) == 1
    row, record = pending(owner)
    assert row.payload_revision == 1 and row.payload_version == 1 and record.uncertainty
    assert record.download_child is None
    with pytest.raises(StateError):
        owner.borrow()
