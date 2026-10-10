"""Actual SQLite, helpers and workstation custody for download checkpoints."""

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
from agentworks.execution._file_download import FileDownloadStatus
from agentworks.execution._file_obligation import (
    DownloadChildAssociation,
    FileCallObligation,
    decode_file_call_obligation,
    encode_file_call_obligation,
)
from agentworks.execution._file_result_transfer import reduce_file_local_download
from agentworks.execution._local_download_publication import LocalDownloadPublication
from agentworks.execution.access import FileAccess
from agentworks.execution.files import Change, Create, Replace
from agentworks.operations import LifecycleObligation, OperationBorrow, OperationOwner
from tests.execution._bound_carrier_support import obligation_receipt
from tests.execution.files._file_access_support import bound_access as bound_access
from tests.execution.files._file_access_support import plan as plan
from tests.execution.files._file_snapshot_support import LocalCarrier

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="actual workstation/helper proof requires Linux")


def associate(access: FileAccess, monkeypatch: pytest.MonkeyPatch) -> DownloadChildAssociation:
    child = DownloadChildAssociation.fresh()
    begin = access._operation.begin_local_download
    monkeypatch.setattr(access._operation, "begin_local_download", lambda: begin(download_child=child))
    return child


def pending(owner: OperationOwner) -> tuple[PersistedLifecycleObligation, FileCallObligation]:
    rows = owner.list_pending_lifecycle_obligations()
    assert len(rows) == 1
    return rows[0], decode_file_call_obligation(rows[0].payload)


@pytest.mark.parametrize("condition", [Create(), Replace()])
def test_original_false_row_before_stage_and_helper_then_success_after_reduction(bound_access, monkeypatch, condition):
    access, root, owner, _db = bound_access
    child = associate(access, monkeypatch)
    (root / "source").write_bytes(b"verified")
    publisher = local._publisher_for_host
    reducer = reduce_file_local_download
    close = OperationBorrow.close
    events = []
    admitted = []

    def check_false() -> None:
        row, record = pending(owner)
        assert row.payload_version == 5
        assert record.download_child == child and not record.download_child_complete
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
        assert record.download_child_complete
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
    assert decode_file_call_obligation(row.payload).download_child_complete


@pytest.mark.parametrize(
    "fault", ["constructor", "commit", "abort", "reduction", "control", "absent", "capture", "allocation"]
)
def test_failed_calls_never_complete_and_incomplete_capture_keeps_original_custody(bound_access, monkeypatch, fault):
    access, root, owner, _db = bound_access
    child = associate(access, monkeypatch)
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
        assert record.download_child == child and not record.download_child_complete
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
            access._operation.settle_download_child_bookkeeping()
        with pytest.raises(StateError):
            access.download(PurePosixPath(source), root.parent / "next")
    else:
        assert row.state is LifecycleObligationState.RESOLVED
    if fault == "constructor":
        assert carrier.calls == 0


@pytest.mark.parametrize(
    "fault",
    ["lost", "unconfirmed", "control", "committed-control", "close-control", "closed-control", "later-revision"],
)
def test_exact_bookkeeping_retry_never_replays_helpers_or_workstation(bound_access, monkeypatch, fault):
    access, root, owner, _db = bound_access
    child = associate(access, monkeypatch)
    (root / "source").write_bytes(b"verified")
    carrier = LocalCarrier()
    monkeypatch.setattr(access, "_carrier", carrier)
    publish = LifecycleObligation.publish_payload
    close = OperationBorrow.close
    intents = []
    enabled = True

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
        if enabled and fault == "later-revision":
            return replace(row, payload_revision=row.payload_revision + 1)
        return row

    def interrupted_close(borrow):
        if enabled and fault == "close-control":
            raise KeyboardInterrupt()
        close(borrow)
        if enabled and fault == "closed-control":
            raise KeyboardInterrupt()

    monkeypatch.setattr(LifecycleObligation, "publish_payload", interrupted)
    monkeypatch.setattr(OperationBorrow, "close", interrupted_close)
    destination = root.parent / "destination"
    if fault == "lost":
        assert access.download(PurePosixPath(root / "source"), destination).size == 8
        assert len(intents) == 2 and intents[0] == intents[1]
    else:
        with pytest.raises(Exception if fault in {"unconfirmed", "later-revision"} else KeyboardInterrupt):
            access.download(PurePosixPath(root / "source"), destination)
        call = access._operation._local_download_call
        assert call is not None
        assert call.active.outcome.status is FileDownloadStatus.COMPLETE
        assert call.local_outcome.published and call.reduction_succeeded
        assert call.association == child and call.publication is not None
        with pytest.raises(StateError):
            access.download(PurePosixPath(root / "source"), root.parent / "next")
        count = carrier.calls
        enabled = False
        owner.stop_admission()
        access._operation.settle_download_child_bookkeeping()
        assert carrier.calls == count and destination.read_bytes() == b"verified"
        assert access._operation._local_download_call is None
        assert len(set(intents)) == 1
    row = obligation_receipt(owner, intents[0][0])
    assert row.state is LifecycleObligationState.RESOLVED
    assert decode_file_call_obligation(row.payload).download_child_complete


@pytest.mark.parametrize("fault", ["fenced", "competing-child", "same-payload-later-revision"])
def test_actual_fencing_and_revision_conflict_keep_original_child(bound_access, monkeypatch, fault):
    access, root, owner, db = bound_access
    associate(access, monkeypatch)
    (root / "source").write_bytes(b"verified")
    publish = LifecycleObligation.publish_payload
    raced = False
    recovered = None

    def race(handle, *, expected_revision, payload_version, payload):
        nonlocal raced, recovered
        if not raced:
            raced = True
            if fault == "fenced":
                recovered = OperationOwner.recover(db.operations, owner.ownership, "f" * 32)
            else:
                record = decode_file_call_obligation(payload)
                other = replace(record, download_child=DownloadChildAssociation.fresh())
                row = publish(
                    handle,
                    expected_revision=expected_revision,
                    payload_version=payload_version,
                    payload=encode_file_call_obligation(other),
                )
                if fault == "same-payload-later-revision":
                    publish(
                        handle, expected_revision=row.payload_revision, payload_version=payload_version, payload=payload
                    )
        return publish(handle, expected_revision=expected_revision, payload_version=payload_version, payload=payload)

    monkeypatch.setattr(LifecycleObligation, "publish_payload", race)
    with pytest.raises(ExternalError) as raised:
        access.download(PurePosixPath(root / "source"), root.parent / "destination")
    assert raised.value.details is not None and raised.value.details.effect is Change.CHANGED
    call = access._operation._local_download_call
    assert call is not None and not call.borrow.closed and call.publication is not None
    with pytest.raises(StateError):
        access._operation.settle_download_child_bookkeeping()
    if recovered is not None:
        assert not decode_file_call_obligation(pending(recovered)[0].payload).download_child_complete


def test_aggregate_settles_captured_child_after_stop_before_component_check(bound_access, monkeypatch):
    from agentworks.execution._execution_operation import ExecutionOperation
    from agentworks.execution.carrier import Deadline
    from agentworks.vms._native_operation import NativeVMOperation, _Workflow
    from tests.execution.files._target_support import target_for_owner

    access, root, owner, db = bound_access
    associate(access, monkeypatch)
    (root / "source").write_bytes(b"verified")
    carrier = LocalCarrier()
    monkeypatch.setattr(access, "_carrier", carrier)
    publish = LifecycleObligation.publish_payload

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(LifecycleObligation, "publish_payload", interrupt)
    with pytest.raises(KeyboardInterrupt):
        access.download(PurePosixPath(root / "source"), root.parent / "destination")
    workflow = _Workflow(owner, Deadline.after(20))
    workflow.views = NativeVMOperation(
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
    monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
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
def test_actual_remote_and_late_local_failures_keep_false_checkpoint(bound_access, monkeypatch, fault):
    from agentworks.execution.carrier import Deadline, ExitStatus

    access, root, owner, _db = bound_access
    associate(access, monkeypatch)
    (root / "source").write_bytes(b"verified")
    commit = LocalDownloadPublication.commit
    abort = LocalDownloadPublication.abort
    enabled = True
    receipts = []

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
    if fault == "uncertain":
        assert original.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert decode_file_call_obligation(original.payload).download_child is not None
    if fault == "cleanup-retry":
        enabled = False
        assert access.download(PurePosixPath(root / "source"), root.parent / "next").size == 8
        assert obligation_receipt(owner, receipts[0]).payload == original.payload
        assert not decode_file_call_obligation(original.payload).download_child_complete
