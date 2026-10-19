"""Actual uploads and SQLite custody for ordinary bookkeeping retries."""

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
    decode_file_call_obligation,
    encode_file_call_obligation,
)
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_publication import Create
from agentworks.execution._file_upload import FileUploadOutcome, FileUploadStatus, _PreparedUpload
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import Carrier, Deadline
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
def held(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[HeldUpload]:
    db = Database(tmp_path / "state.db")
    kind = getattr(request, "param", OperationResourceKind.VM)
    name = "child-vm" if kind is OperationResourceKind.VM else "child-host"
    owner = OperationOwner.acquire(db.operations, OperationScope(kind, name), "child-copy")
    target = owner.ownership.scope if kind is OperationResourceKind.PLATFORM_HOST else target_for_owner(owner)
    operation = FileOperation(owner, target)
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
    )


@pytest.mark.parametrize("held", list(OperationResourceKind), indirect=True)
def test_success_closes_original_admission_without_history(held, monkeypatch):
    db, owner, operation, root, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    close = OperationBorrow.close
    observed = []

    def closing(borrow):
        (row,) = owner.list_pending_lifecycle_obligations()
        observed.append(row)
        assert row.payload_version == (
            7 if owner.ownership.scope.resource_kind is OperationResourceKind.PLATFORM_HOST else 1
        )
        assert row.payload_revision == 0
        assert decode_file_call_obligation(row.payload).upload_child is None
        close(borrow)

    def forbidden(*args, **kwargs):
        raise AssertionError("successful ordinary upload published history")

    monkeypatch.setattr(OperationBorrow, "close", closing)
    monkeypatch.setattr(LifecycleObligation, "publish_payload", forbidden)
    result = upload(held, carrier, BytesSource(b"actual"))
    assert result.status is FileUploadStatus.COMPLETE
    assert len(observed) == 1 and not operation.active_uploads
    row = obligation_receipt(owner, observed[0].obligation_id)
    assert row.state is LifecycleObligationState.RESOLVED and row.payload_revision == 0
    assert root.joinpath("target").read_bytes() == b"actual"


@pytest.mark.parametrize(
    ("debt", "fault"),
    [(True, fault) for fault in ["lost", "unconfirmed", "control", "committed-control", "close", "closed", "retention"]]
    + [(False, "close"), (False, "closed")],
)
@pytest.mark.parametrize("held", list(OperationResourceKind), indirect=True)
def test_exact_bookkeeping_retry_without_helper_or_source_replay(held, monkeypatch, fault, debt):
    db, owner, operation, root, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    if debt:
        monkeypatch.setattr(carrier, "_inner", LostThenRuntimeRefusalCarrier())
    source = BytesSource(b"actual")
    publish = LifecycleObligation.publish_payload
    release = OperationBorrow.handoff_retained_effect if debt else OperationBorrow.close
    retain = operation._retain_unfinished_upload
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
        return row

    def interrupted_release(borrow):
        if enabled and fault == "close":
            raise KeyboardInterrupt()
        release(borrow)
        if enabled and fault == "closed":
            raise KeyboardInterrupt()

    def interrupted_retention(unfinished):
        retain(unfinished)
        if enabled and fault == "retention":
            raise KeyboardInterrupt()

    monkeypatch.setattr(LifecycleObligation, "publish_payload", interrupted)
    monkeypatch.setattr(OperationBorrow, "handoff_retained_effect" if debt else "close", interrupted_release)
    monkeypatch.setattr(operation, "_retain_unfinished_upload", interrupted_retention)
    fails = fault in {"close", "closed"} or (debt and fault != "lost")
    if fails:
        with pytest.raises(RuntimeError if fault == "unconfirmed" else KeyboardInterrupt):
            upload(held, carrier, source)
        (active,) = operation.active_uploads
        assert active.outcome is not None
        assert active.outcome.requires_owner_retention is debt
        assert active.borrow.closed is (fault == "closed")
        with pytest.raises(StateError):
            upload(held, carrier, BytesSource(b"next"), path="next")
        assert not root.joinpath("next").exists()
    else:
        result = upload(held, carrier, source)
        assert result.requires_owner_retention is debt
    activity = len(carrier.payloads), source.calls
    enabled = False
    owner.stop_admission()
    operation.settle_upload_bookkeeping()
    operation.settle_upload_bookkeeping()
    assert (len(carrier.payloads), source.calls) == activity
    assert not operation.active_uploads
    assert len(operation.unfinished_uploads) == int(debt)
    row = obligation_receipt(owner, next(iter(carrier.receipt_ids)))
    assert row.payload_revision == int(debt)
    assert row.payload_version == (
        7 if owner.ownership.scope.resource_kind is OperationResourceKind.PLATFORM_HOST else 1
    )
    record = decode_file_call_obligation(row.payload)
    assert record.upload_child is None
    if debt:
        assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
        assert record.token == operation.unfinished_uploads[0].outcome.token
        assert record.uncertainty
        assert len(set(intents)) == 1
        if fails:
            assert operation.unfinished_uploads[0].outcome is active.outcome
    else:
        assert not intents and row.state is LifecycleObligationState.RESOLVED
        assert root.joinpath("target").read_bytes() == source.data
    with pytest.raises(StateError):
        owner.borrow()


@pytest.mark.parametrize("fault", ["fenced", "conflict", "later-revision"])
def test_debt_revision_conflict_or_fencing_keeps_exact_custody(held, monkeypatch, fault):
    db, owner, operation, _, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    monkeypatch.setattr(carrier, "_inner", LostThenRuntimeRefusalCarrier())
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
                other = replace(record, relative_path="other")
                row = publish(
                    handle,
                    expected_revision=expected_revision,
                    payload_version=payload_version,
                    payload=encode_file_call_obligation(other),
                )
                if fault == "later-revision":
                    publish(
                        handle, expected_revision=row.payload_revision, payload_version=payload_version, payload=payload
                    )
        return publish(handle, expected_revision=expected_revision, payload_version=payload_version, payload=payload)

    monkeypatch.setattr(LifecycleObligation, "publish_payload", race)
    with pytest.raises(StateError):
        upload(held, carrier, BytesSource(b"actual"))
    (active,) = operation.active_uploads
    intent = active.retained_payload
    assert intent is not None and active.outcome.requires_owner_retention
    with pytest.raises(StateError):
        operation.settle_upload_bookkeeping()
    assert not active.borrow.closed and not operation.unfinished_uploads
    assert active.retained_payload is intent
    with pytest.raises(StateError):
        owner.borrow()


@pytest.mark.parametrize("path", ["../escape", "/absolute", "target/../alias"])
def test_invalid_path_refuses_before_admission_and_source_read(held, path):
    db, owner, operation, _, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    source = BytesSource(b"actual")
    with pytest.raises(ValidationError):
        upload(held, carrier, source, path=path)
    assert not carrier.payloads and source.calls == 0 and not operation.active_uploads
    assert owner.list_pending_lifecycle_obligations() == ()


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("held", list(OperationResourceKind), indirect=True)
def test_original_control_survives_debt_publication_interruption(held, monkeypatch, committed):
    from agentworks.execution._file_upload import FileUploadControlFact

    db, owner, operation, _, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    monkeypatch.setattr(carrier, "_inner", LostThenRuntimeRefusalCarrier())
    run = _PreparedUpload.run
    primary = KeyboardInterrupt()
    secondary = SystemExit()
    publish = LifecycleObligation.publish_payload

    def controlled(prepared):
        outcome = run(prepared)
        raise primary from FileUploadControlFact(outcome)

    def interrupted(handle, **kwargs):
        if committed:
            publish(handle, **kwargs)
        raise secondary

    monkeypatch.setattr(_PreparedUpload, "run", controlled)
    monkeypatch.setattr(LifecycleObligation, "publish_payload", interrupted)
    with pytest.raises(KeyboardInterrupt) as raised:
        upload(held, carrier, BytesSource(b"actual"))
    assert raised.value is primary and isinstance(primary.__cause__, FileUploadControlFact)
    (active,) = operation.active_uploads
    assert active.outcome is primary.__cause__.outcome
    monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
    operation.settle_upload_bookkeeping()
    assert operation.unfinished_uploads[0].outcome is active.outcome


def test_incomplete_capture_refuses_bookkeeping_without_repeating_file_work(held, monkeypatch):
    from agentworks.execution import _file_operation as module

    db, owner, operation, _, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    monkeypatch.setattr(carrier, "_inner", LostThenRuntimeRefusalCarrier())
    source = BytesSource(b"actual")

    def fail(*args):
        raise MemoryError()

    with monkeypatch.context() as patch:
        patch.setattr(module, "encode_file_call_obligation", fail)
        with pytest.raises(MemoryError):
            upload(held, carrier, source)
    activity = len(carrier.payloads), source.calls
    with pytest.raises(StateError):
        operation.settle_upload_bookkeeping()
    (active,) = operation.active_uploads
    assert active.outcome.requires_owner_retention and not active.capture_ready
    assert not active.borrow.closed and (len(carrier.payloads), source.calls) == activity


def test_aggregate_retries_closure_before_component_check(held, monkeypatch):
    from agentworks.execution._execution_operation import ExecutionOperation
    from agentworks.vms._native_operation import VMOperation, _Workflow

    db, owner, operation, _, _ = held
    carrier = ObligationInspectingCarrier(db, owner)
    close = OperationBorrow.close

    def interrupt(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(OperationBorrow, "close", interrupt)
    with pytest.raises(KeyboardInterrupt):
        upload(held, carrier, BytesSource(b"settled"))
    workflow = _Workflow(owner, Deadline.after(20))
    workflow.views = VMOperation(owner, None, None, operation, ExecutionOperation(owner, target_for_owner(owner)), None)
    with pytest.raises(KeyboardInterrupt):
        workflow.close()
    monkeypatch.setattr(OperationBorrow, "close", close)
    calls = len(carrier.payloads)
    workflow.close(cleanup_deadline=Deadline.after(20))
    assert not operation.active_uploads and len(carrier.payloads) == calls
    assert db.operations.inspect(owner.ownership.scope) is None
