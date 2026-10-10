"""Real uploads and SQLite custody for bounded core child checkpoints."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._file_obligation import (
    UploadChildAssociation,
    decode_file_call_obligation,
    encode_file_call_obligation,
)
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_publication import Create
from agentworks.execution._file_upload import FileUploadOutcome, FileUploadStatus, _PreparedUpload
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import Carrier, Deadline, ExitStatus
from agentworks.operations import LifecycleObligation, OperationBorrow, OperationOwner
from tests.execution._bound_carrier_support import obligation_receipt
from tests.execution.files._file_publication_support import install_fixture_bundle as install_publication_bundle
from tests.execution.files._file_read_support import install_fixture_bundle as install_read_bundle
from tests.execution.files._file_stage_support import install_fixture_bundle as install_stage_bundle
from tests.execution.files._file_upload_support import BytesSource
from tests.execution.files._runtime_support import runtime_selection
from tests.execution.files._target_support import target_for_owner
from tests.execution.files.test_file_operation_write import ObligationInspectingCarrier
from tests.execution.files.test_file_upload import LostThenRuntimeRefusalCarrier

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="local helper upload proof requires Linux")
type HeldUpload = tuple[Database, OperationOwner, FileOperation, Path, IdentityPlan]


@pytest.fixture(autouse=True)
def bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    install_stage_bundle(monkeypatch)
    install_publication_bundle(monkeypatch)
    install_read_bundle(monkeypatch)


@pytest.fixture
def held(tmp_path: Path) -> Iterator[HeldUpload]:
    db = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(db.operations, OperationScope(OperationResourceKind.VM, "child-vm"), "child-copy")
    operation = FileOperation(owner, target_for_owner(owner))
    root = tmp_path / "approved"
    root.mkdir()
    gid = os.getegid()
    plan = IdentityPlan(
        IdentityExpectation(os.geteuid(), gid, tuple(sorted(set(os.getgroups()) | {gid}))), IdentityMode.DIRECT
    )
    try:
        yield db, owner, operation, root, plan
    finally:
        assert owner.close_local_delivery(Deadline.after(3))
        db.close()


def upload(
    held: HeldUpload,
    carrier: Carrier,
    source: BytesSource,
    child: UploadChildAssociation | None,
    *,
    path="target",
    deadline=None,
) -> FileUploadOutcome:
    _, _, operation, root, plan = held
    from tests.execution.files._file_upload_support import new_metadata

    return operation.upload(
        carrier,
        trusted_root_path=str(root),
        relative_path=path,
        source=source,
        size=len(source.data),
        condition=Create(),
        create_metadata=new_metadata(),
        plan=plan,
        deadline=Deadline.after(20) if deadline is None else deadline,
        runtime_selection=runtime_selection(sys.executable),
        upload_child=child,
    )


@pytest.mark.parametrize("associated", [False, True])
def test_exact_child_admission_and_success_before_borrow_close(
    held, monkeypatch: pytest.MonkeyPatch, associated: bool
) -> None:
    db, owner, operation, root, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    child = UploadChildAssociation.fresh() if associated else None
    close = OperationBorrow.close
    observed = []

    def inspect_close(borrow: OperationBorrow) -> None:
        rows = db.operations.list_pending_lifecycle_obligations(owner.ownership)
        assert len(rows) == 1
        record = decode_file_call_obligation(rows[0].payload)
        observed.append(record)
        assert record.upload_child == child
        assert record.upload_child_complete is associated
        assert rows[0].payload_version == (3 if associated else 1)
        if associated:
            assert record.scratch_reference is not None
            assert record.token == carrier.payloads[0].token
        close(borrow)

    monkeypatch.setattr(OperationBorrow, "close", inspect_close)
    result = upload(held, carrier, BytesSource(b"private-file-content"), child)
    assert result.status is FileUploadStatus.COMPLETE
    assert len(observed) == 1
    assert all(record.upload_child == child and not record.upload_child_complete for record in carrier.payloads)
    row = obligation_receipt(owner, next(iter(carrier.receipt_ids)))
    assert row.state is LifecycleObligationState.RESOLVED
    assert decode_file_call_obligation(row.payload) == observed[0]
    assert root.joinpath("target").read_bytes() == b"private-file-content"
    assert operation.active_uploads == ()


@pytest.mark.parametrize("failure", ["refused", "nonzero", "uncertain", "deadline"])
def test_unsuccessful_children_never_gain_completion(held, failure: str) -> None:
    db, owner, operation, root, _ = held

    class FailedCarrier(ObligationInspectingCarrier):
        def execute(self, invocation, *, io, deadline, custody=None):
            report = super().execute(invocation, io=io, deadline=deadline, custody=custody)
            if len(self.payloads) == 1:
                if failure == "nonzero":
                    return replace(report, completion=ExitStatus(code=1))
                if failure == "uncertain":
                    return replace(report, stdout=replace(report.stdout, complete=False))
            return report

    carrier = FailedCarrier(db, owner)
    if failure == "refused":
        root.joinpath("target").write_bytes(b"original")
    outcome = upload(
        held,
        carrier,
        BytesSource(b"new"),
        UploadChildAssociation.fresh(),
        deadline=Deadline.after(0) if failure == "deadline" else None,
    )
    assert outcome.status is not FileUploadStatus.COMPLETE
    assert not operation.active_uploads
    for record in carrier.payloads:
        assert not record.upload_child_complete
    for receipt_id in carrier.receipt_ids:
        record = decode_file_call_obligation(obligation_receipt(owner, receipt_id).payload)
        assert record.upload_child is not None and not record.upload_child_complete


@pytest.mark.parametrize(
    "interruption",
    [
        "lost-reply",
        "unconfirmed",
        "publish-control",
        "publish-committed-control",
        "close-control",
        "close-lost-reply",
        "locally-closed-control",
    ],
)
def test_checkpoint_retry_keeps_original_child_without_more_file_activity(
    held, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    db, owner, operation, root, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    source = BytesSource(b"actual-child")
    child = UploadChildAssociation.fresh()
    publish = LifecycleObligation.publish_payload
    resolve = type(db.operations).resolve_lifecycle_obligation
    close = OperationBorrow.close
    requests = []
    enabled = True
    control = KeyboardInterrupt()

    def interrupted_publish(handle, *, expected_revision, payload_version, payload):
        requests.append((handle.obligation_id, expected_revision, payload_version, payload))
        if enabled and interruption == "unconfirmed":
            raise RuntimeError("unconfirmed database request")
        if enabled and interruption == "publish-control":
            raise control
        result = publish(handle, expected_revision=expected_revision, payload_version=payload_version, payload=payload)
        if enabled and interruption == "publish-committed-control":
            raise control
        if enabled and interruption == "lost-reply" and len(requests) == 1:
            raise RuntimeError("committed without reply")
        return result

    def interrupted_resolve(repository, ownership, obligation_id):
        if enabled and interruption == "close-control":
            raise control
        result = resolve(repository, ownership, obligation_id)
        if enabled and interruption == "close-lost-reply":
            raise control
        return result

    def interrupted_close(borrow):
        close(borrow)
        if enabled and interruption == "locally-closed-control":
            raise control

    monkeypatch.setattr(LifecycleObligation, "publish_payload", interrupted_publish)
    monkeypatch.setattr(type(db.operations), "resolve_lifecycle_obligation", interrupted_resolve)
    monkeypatch.setattr(OperationBorrow, "close", interrupted_close)
    if interruption == "lost-reply":
        assert upload(held, carrier, source, child).status is FileUploadStatus.COMPLETE
        assert len(requests) == 2 and requests[0] == requests[1]
    else:
        with pytest.raises(RuntimeError if interruption == "unconfirmed" else KeyboardInterrupt):
            upload(held, carrier, source, child)
        assert len(operation.active_uploads) == 1
        active = operation.active_uploads[0]
        assert active.outcome is not None and active.outcome.status is FileUploadStatus.COMPLETE
        assert active.binding.relative_path == "target"
        assert active.borrow.closed is (interruption == "locally-closed-control")
        with pytest.raises(StateError):
            upload(held, carrier, BytesSource(b"forbidden-next-child"), replace(child, member_ordinal=1), path="next")
        assert not root.joinpath("next").exists()
    activity = (len(carrier.payloads), source.calls)
    receipt_id = next(iter(carrier.receipt_ids))
    enabled = False
    owner.stop_admission()
    operation.settle_upload_child_bookkeeping()
    operation.settle_upload_child_bookkeeping()
    assert (len(carrier.payloads), source.calls) == activity
    assert not operation.active_uploads and not operation.unfinished_uploads
    row = obligation_receipt(owner, receipt_id)
    assert row.state is LifecycleObligationState.RESOLVED and row.payload_revision == 1
    record = decode_file_call_obligation(row.payload)
    assert record.upload_child == child and record.upload_child_complete
    assert record.token == carrier.payloads[0].token and record.relative_path == "target"
    assert root.joinpath("target").read_bytes() == source.data
    assert all(request == requests[0] for request in requests)
    with pytest.raises(StateError):
        owner.borrow()


@pytest.mark.parametrize("same_payload_later_revision", [False, True])
def test_conflicting_revision_cannot_confirm_or_release_child(
    held, monkeypatch: pytest.MonkeyPatch, same_payload_later_revision: bool
) -> None:
    db, owner, operation, _, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    publish = LifecycleObligation.publish_payload
    raced = False

    def race(handle, *, expected_revision, payload_version, payload):
        nonlocal raced
        if not raced:
            raced = True
            record = decode_file_call_obligation(payload)
            other = replace(record, upload_child=UploadChildAssociation.fresh())
            row = publish(
                handle,
                expected_revision=expected_revision,
                payload_version=payload_version,
                payload=encode_file_call_obligation(other),
            )
            if same_payload_later_revision:
                publish(
                    handle, expected_revision=row.payload_revision, payload_version=payload_version, payload=payload
                )
        return publish(handle, expected_revision=expected_revision, payload_version=payload_version, payload=payload)

    monkeypatch.setattr(LifecycleObligation, "publish_payload", race)
    with pytest.raises(StateError):
        upload(held, carrier, BytesSource(b"actual"), UploadChildAssociation.fresh())
    assert operation.active_uploads
    with pytest.raises(StateError):
        operation.settle_upload_child_bookkeeping()
    with pytest.raises(StateError):
        owner.borrow()


def test_fenced_owner_cannot_publish_checkpoint_or_release_original_child(
    held, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, owner, operation, _, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    publish = LifecycleObligation.publish_payload
    recovered: OperationOwner | None = None

    def fenced_publish(handle, **kwargs):
        nonlocal recovered
        if recovered is None:
            recovered = OperationOwner.recover(db.operations, owner.ownership, "f" * 32)
        return publish(handle, **kwargs)

    monkeypatch.setattr(LifecycleObligation, "publish_payload", fenced_publish)
    with pytest.raises(StateError):
        upload(held, carrier, BytesSource(b"actual"), UploadChildAssociation.fresh())
    assert recovered is not None and operation.active_uploads
    (row,) = recovered.list_pending_lifecycle_obligations()
    assert not decode_file_call_obligation(row.payload).upload_child_complete
    with pytest.raises(StateError):
        operation.settle_upload_child_bookkeeping()
    assert not operation.active_uploads[0].borrow.closed


def test_late_deadline_with_confirmed_publication_does_not_checkpoint_success(
    held, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, owner, operation, _, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    run = _PreparedUpload.run

    def late(prepared: _PreparedUpload) -> FileUploadOutcome:
        outcome = run(prepared)
        assert outcome.status is FileUploadStatus.COMPLETE and outcome.publication_confirmed
        return replace(outcome, deadline_exceeded=True)

    monkeypatch.setattr(_PreparedUpload, "run", late)
    outcome = upload(held, carrier, BytesSource(b"published"), UploadChildAssociation.fresh())
    assert outcome.deadline_exceeded and not operation.active_uploads
    row = obligation_receipt(owner, next(iter(carrier.receipt_ids)))
    assert row.state is LifecycleObligationState.RESOLVED
    assert not decode_file_call_obligation(row.payload).upload_child_complete


def test_bookkeeping_retry_preserves_actual_remote_uncertainty(held, monkeypatch: pytest.MonkeyPatch) -> None:
    db, owner, operation, _, _ = held
    publish = LifecycleObligation.publish_payload

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    carrier = ObligationInspectingCarrier(db, owner)
    # Lose the actual stage-BEGIN reply, then refuse its reconciliation.
    # A lost BEGIN alone can be reconciled and cleaned by the ordinary upload.
    monkeypatch.setattr(carrier, "_inner", LostThenRuntimeRefusalCarrier())
    source = BytesSource(b"actual")
    monkeypatch.setattr(LifecycleObligation, "publish_payload", interrupt)
    with pytest.raises(KeyboardInterrupt):
        upload(held, carrier, source, UploadChildAssociation.fresh())
    (active,) = operation.active_uploads
    assert active.outcome is not None and active.outcome.requires_owner_retention
    activity = len(carrier.payloads), source.calls
    with pytest.raises(StateError):
        owner.borrow()
    owner.stop_admission()
    monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
    operation.settle_upload_child_bookkeeping()
    operation.settle_upload_child_bookkeeping()
    assert active.borrow.closed and len(operation.unfinished_uploads) == 1
    assert operation.unfinished_uploads[0].outcome is active.outcome
    assert not operation.active_uploads and (len(carrier.payloads), source.calls) == activity
    (row,) = owner.list_pending_lifecycle_obligations()
    assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
    assert not decode_file_call_obligation(row.payload).upload_child_complete
    with pytest.raises(StateError):
        owner.borrow()


@pytest.mark.parametrize("path", ["../escape", "/absolute", "target/../alias"])
def test_invalid_child_path_refuses_before_admission_and_source_read(held, path: str) -> None:
    db, owner, operation, _, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    source = BytesSource(b"actual")
    with pytest.raises(ValidationError):
        upload(held, carrier, source, UploadChildAssociation.fresh(), path=path)
    assert not carrier.payloads and source.calls == 0 and not operation.active_uploads
    assert owner.list_pending_lifecycle_obligations() == ()


def test_aggregate_cleanup_retries_child_before_component_check(held, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.execution._execution_operation import ExecutionOperation
    from agentworks.vms._native_operation import NativeVMOperation, _Workflow

    db, owner, operation, _, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    control = KeyboardInterrupt()
    publish = LifecycleObligation.publish_payload

    def interrupt(*args, **kwargs):
        raise control

    monkeypatch.setattr(LifecycleObligation, "publish_payload", interrupt)
    with pytest.raises(KeyboardInterrupt):
        upload(held, carrier, BytesSource(b"settled"), UploadChildAssociation.fresh())
    workflow = _Workflow(owner, Deadline.after(20))
    workflow.views = NativeVMOperation(
        owner, None, None, operation, ExecutionOperation(owner, target_for_owner(owner)), None
    )
    with pytest.raises(KeyboardInterrupt):
        workflow.close()
    assert operation.active_uploads
    monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
    calls = len(carrier.payloads)
    workflow.close(cleanup_deadline=Deadline.after(20))
    assert not operation.active_uploads and len(carrier.payloads) == calls
    assert db.operations.inspect(owner.ownership.scope) is None
