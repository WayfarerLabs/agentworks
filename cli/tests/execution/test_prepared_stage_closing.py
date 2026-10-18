"""Exact stage cleanup after ordinary operation admission has stopped."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_stage_protocol import (
    FileStageCleanupRequest,
    FileStageOperation,
    FileStageReconcileRequest,
    decode_file_stage_request,
    empty_file_stage_body,
    encode_file_stage_cleanup_result,
    encode_file_stage_reconcile_result,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_launcher import IdentityMode
from agentworks.execution._prepared_helper_obligation import (
    PREPARED_HELPER_OBLIGATION_KIND,
    decode_prepared_helper_obligation,
    encode_prepared_helper_obligation,
)
from agentworks.execution._prepared_stage_closing import PreparedStageCleanup
from agentworks.execution._runtime_prerequisite import HelperClosureExpectation
from agentworks.execution._scratch_receipt import ScratchHistoricalOwnership, ScratchOwnershipUncertainty
from agentworks.execution._vm_guest_identity_protocol import vm_guest_boot_id
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.operations import LifecycleObligation, OperationOwner
from tests.execution.test_prepared_helper_stage import BOOTSTRAP, TARGET, Carrier, pending, stage


class ScriptedCarrier(Carrier):
    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        if not self.owner._close_requested.is_set():
            return super().execute(invocation, io=io, deadline=deadline, custody=custody)
        assert custody is not None
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        request = decode_file_stage_request(io.input.data[io.input.data.index(b"{") :])
        assert isinstance(request, (FileStageCleanupRequest, FileStageReconcileRequest))
        assert self.owner._active_borrow is None and self.owner._outstanding_attempt is None
        assert PREPARED_HELPER_OBLIGATION_KIND in pending(self.owner) and "carrier-dispatch" not in pending(self.owner)
        self.requests.append(request)
        self.deadlines.append(deadline)
        self.custody = custody
        if self.unsettled:
            custody.begin_process()
        if self.error is not None:
            raise self.error
        if isinstance(request, FileStageCleanupRequest):
            body = encode_file_stage_cleanup_result()
        else:
            assert self.reference is not None
            body = encode_file_stage_reconcile_result(
                ScratchOwnershipUncertainty()
                if self.ownership_uncertain
                else ScratchHistoricalOwnership(self.reference._ownership)
            )
        if self.body_override is not None:
            body = self.body_override
        payload = f"AGW_RUNTIME_1:{request.nonce}:ready:0\n".encode() + b"".join(
            encode_file_record(request.nonce, FileRecord(index, kind, data))
            for index, (kind, data) in enumerate(
                ((FileRecordKind.RESULT, body), (FileRecordKind.FINISHED, empty_file_stage_body()))
            )
        )
        io.output.stdout.try_write(memoryview(payload))
        if self.before_return is not None:
            self.before_return()
        channel = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(
            self.dispatch,
            ExitStatus(self.code) if self.code is not None and self.dispatch is Dispatch.SENT else None,
            stdout=channel,
            stderr=channel,
        )


@pytest.fixture
def owned(tmp_path: Path) -> Generator[tuple[Database, OperationOwner, ScriptedCarrier]]:
    database = Database(tmp_path / "closing.db")
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, TARGET.name), "test")
    try:
        yield database, owner, ScriptedCarrier(owner)
    finally:
        database.close()


def test_passive_controller_then_ordinary_cleanup(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    cleanup = PreparedStageCleanup(prepared)
    assert carrier.requests == [] and pending(owner) == []
    assert prepared.begin()
    assert cleanup.finish(Deadline.after(30)) and prepared.cleaned and pending(owner) == []
    assert [request.operation for request in carrier.requests] == [FileStageOperation.BEGIN, FileStageOperation.CLEANUP]


def test_cleanup_after_stopped_admission_keeps_ordinary_borrows_closed(owned) -> None:
    _, owner, carrier = owned
    other = owner.register_lifecycle_obligation("other", payload_version=1, payload=b"{}")
    other.mark_possible_effect()
    prepared = stage(owned)
    cleanup = PreparedStageCleanup(prepared)
    assert prepared.begin() and prepared.append(b"abc")
    owner.stop_admission()
    with pytest.raises(StateError):
        owner.borrow()
    assert cleanup.finish(Deadline.after(30))
    assert prepared.cleaned and prepared.failed and pending(owner) == ["other"]
    assert cleanup.finish(Deadline.after(30)) and len(carrier.requests) == 3
    with pytest.raises(StateError):
        owner.borrow()
    for action in (lambda: prepared.append(b"abc"), lambda: prepared.prepare_activation("a" * 32, b"d" * 32)):
        with pytest.raises(StateError):
            action()


def test_partial_begin_uses_token_only_as_cleanup_debt(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    carrier.body_override = b"{}"
    assert not prepared.begin() and prepared.reference is None
    carrier.body_override = None
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()
    assert cleanup.finish(Deadline.after(30)) and pending(owner) == []
    assert prepared.reference is None
    assert [request.operation for request in carrier.requests] == [
        FileStageOperation.BEGIN,
        FileStageOperation.RECONCILE,
        FileStageOperation.CLEANUP,
    ]


def test_missing_receipt_is_never_stage_absence_proof(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    prepared.cleanup_debt = None
    carrier.ownership_uncertain = True
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()
    assert not cleanup.finish(Deadline.after(30))
    assert not prepared.cleaned and pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]
    assert len(carrier.requests) == 2 and prepared.cleanup_debt is None


@pytest.mark.parametrize("code", [None, 1, 255])
def test_unknown_closing_helper_blocks_next_helper_even_after_local_drain(owned, code) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()
    carrier.code, carrier.unsettled = code, True
    assert not cleanup.finish(Deadline.after(30))
    call = cleanup._active
    assert call is not None and call.helper.custody is carrier.custody and call.helper.custody.settled
    for _ in range(2):
        assert not cleanup.finish(Deadline.after(30))
    assert cleanup._active is call and len(carrier.requests) == 2
    assert not prepared.cleaned and pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]


class NativeDelivery(ScriptedCarrier):
    def __init__(self, source: ScriptedCarrier, expectation: HelperClosureExpectation) -> None:
        super().__init__(source.owner)
        self.reference = source.reference
        self.expectation = expectation
        self.proven = False
        self.error = source.error
        self.unsettled = True
        self.observed: list[tuple[Deadline, LocalDeliveryCustody]] = []

    @property
    def closure_proven(self) -> bool:
        return self.proven

    def observe_closure(self, *, deadline: Deadline, custody: LocalDeliveryCustody) -> bool:
        assert custody is self.custody and custody.settled
        self.observed.append((deadline, custody))
        return self.proven


def test_native_closure_false_then_true_reuses_exact_call_and_fresh_budget(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    deliveries: list[NativeDelivery] = []
    routes: list[Deadline] = []

    def factory(expectation: HelperClosureExpectation) -> NativeDelivery:
        delivery = NativeDelivery(carrier, expectation)
        deliveries.append(delivery)
        return delivery

    prepared._new_helper_delivery = factory
    prepared._before_dispatch = routes.append
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()
    first = Deadline.after(30)
    assert not cleanup.finish(first)
    delivery = deliveries[0]
    assert delivery.requests[0].nonce == delivery.expectation.nonce
    assert delivery.expectation.guest == BOOTSTRAP.guest and delivery.expectation.candidates == ("/usr/bin/python3",)
    fresh = Deadline.after(30)
    assert not cleanup.finish(fresh)
    assert delivery.observed == [(fresh, delivery.custody)]
    assert routes == [first, fresh] and len(delivery.requests) == 1
    delivery.proven = True
    assert cleanup.finish(fresh) and prepared.cleaned and pending(owner) == []
    assert len(deliveries) == 1 and len(delivery.requests) == 1


def test_lost_cleanup_candidate_retries_removal_only_after_exact_native_closure(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    deliveries: list[NativeDelivery] = []
    control = KeyboardInterrupt("cleanup delivery interrupted")
    carrier.error = control

    def factory(expectation: HelperClosureExpectation) -> NativeDelivery:
        delivery = NativeDelivery(carrier, expectation)
        delivery.proven = bool(deliveries)
        deliveries.append(delivery)
        return delivery

    prepared._new_helper_delivery = factory
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()
    with pytest.raises(KeyboardInterrupt) as caught:
        cleanup.finish(Deadline.after(30))
    assert caught.value is control and cleanup._active is not None and cleanup._active.candidate is None
    first = deliveries[0]
    assert not cleanup.finish(Deadline.after(30)) and len(deliveries) == 1
    assert pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND] and not prepared.cleaned
    first.proven = True
    carrier.error = None
    assert cleanup.finish(Deadline.after(30))
    assert len(deliveries) == 2 and all(len(delivery.requests) == 1 for delivery in deliveries)
    assert all(
        request.operation is FileStageOperation.CLEANUP for delivery in deliveries for request in delivery.requests
    )
    assert pending(owner) == []
    assert all(value is not control for value in vars(cleanup).values())


@pytest.mark.parametrize(
    "changed", ["guest", "root", "runtime", "token", "row", "row_guest", "row_state", "generation"]
)
def test_changed_binding_refuses_closing_dispatch(owned, monkeypatch, changed) -> None:
    database, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()
    if changed == "guest":
        prepared._bootstrap = replace(BOOTSTRAP, guest=replace(BOOTSTRAP.guest, init_start_ticks=999))
    elif changed == "root":
        prepared._bootstrap = replace(BOOTSTRAP, root_entry=replace(BOOTSTRAP.root_entry, mode=IdentityMode.DIRECT))
    elif changed == "runtime":
        prepared._runtime = replace(prepared._runtime, explicit_path=None)
    elif changed == "token":
        prepared.token = b"x" * 16
    elif changed == "generation":
        OperationOwner.recover(database.operations, owner.ownership, "e" * 32)
    else:
        original = owner.inspect_lifecycle_obligation

        def changed_row(identity):
            row = original(identity)
            assert row is not None
            if changed == "row_state":
                return replace(row, state=LifecycleObligationState.RESOLVED)
            if changed == "row_guest":
                record = decode_prepared_helper_obligation(row.payload)
                guest = replace(record.bootstrap.guest, init_start_ticks=999)
                changed_record = replace(
                    record,
                    bootstrap=replace(record.bootstrap, guest=guest),
                    target=replace(record.target, boot_id=vm_guest_boot_id(guest)),
                )
                return replace(row, payload=encode_prepared_helper_obligation(changed_record))
            return replace(row, payload_revision=1)

        monkeypatch.setattr(owner, "inspect_lifecycle_obligation", changed_row)
    with pytest.raises(StateError):
        cleanup.finish(Deadline.after(30))
    assert len(carrier.requests) == 1 and not prepared.cleaned


@pytest.mark.parametrize("phase", ["borrow", "activation"])
def test_unsettled_owner_or_activation_blocks_closing(owned, phase) -> None:
    _, owner, carrier = owned
    prepared = stage(owned, 0)
    assert prepared.begin()
    borrow = None
    if phase == "borrow":
        borrow = owner.borrow()
    else:
        prepared.prepare_activation("a" * 32, b"d" * 32)
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()
    with pytest.raises(StateError):
        cleanup.finish(Deadline.after(30))
    assert len(carrier.requests) == 1
    if borrow is not None:
        borrow.close()


@pytest.mark.parametrize("proof", ["closed", "not_dispatched"])
def test_explicit_parent_activation_proofs_allow_cleanup_without_fabricated_status(owned, proof) -> None:
    _, owner, carrier = owned
    prepared = stage(owned, 0)
    assert prepared.begin()
    custody = LocalDeliveryCustody()
    with pytest.raises(StateError):
        prepared.note_activation_closure(custody)
    with pytest.raises(StateError):
        prepared.note_activation_not_dispatched()
    prepared.prepare_activation("a" * 32, b"d" * 32)
    if proof == "closed":
        custody.begin_process()
        with pytest.raises(StateError):
            prepared.note_activation_closure(custody)
        assert custody.close(Deadline.after(30))
        prepared.note_activation_closure(custody)
    else:
        prepared.note_activation_not_dispatched()
    with pytest.raises(StateError):
        prepared.note_activation_not_dispatched()
    with pytest.raises(StateError):
        prepared.note_activation_closure(custody)
    owner.stop_admission()
    assert PreparedStageCleanup(prepared).finish(Deadline.after(30)) and len(carrier.requests) == 2


@pytest.mark.parametrize("committed", [False, True])
def test_cleanup_resolution_reply_retries_only_same_local_row(owned, monkeypatch, committed) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()
    original = LifecycleObligation.resolve

    def interrupted(obligation):
        assert prepared.cleaned and prepared.cleanup_debt is None
        if committed:
            original(obligation)
        raise KeyboardInterrupt("cleanup resolution reply lost")

    with monkeypatch.context() as patch:
        patch.setattr(LifecycleObligation, "resolve", interrupted)
        with pytest.raises(KeyboardInterrupt):
            cleanup.finish(Deadline.after(30))
    assert prepared.cleaned and prepared.cleanup_resolution_uncertain
    assert cleanup.finish(Deadline.after(30))
    assert not prepared.cleanup_resolution_uncertain and pending(owner) == [] and len(carrier.requests) == 2


def test_original_expiry_does_not_change_fresh_closing_route_budget(owned, monkeypatch) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    routes: list[Deadline] = []
    prepared._before_dispatch = routes.append
    monkeypatch.setattr(prepared, "_deadline", Deadline.after(0))
    owner.stop_admission()
    fresh = Deadline.after(30)
    assert PreparedStageCleanup(prepared).finish(fresh)
    assert routes == [fresh] and carrier.deadlines[-1] is fresh


def test_cleanup_drains_existing_ordinary_stage_call_before_closing_effect(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    carrier.unsettled = True
    assert not prepared.begin() and prepared.helper_uncertain
    owner.stop_admission()
    cleanup = PreparedStageCleanup(prepared)
    assert cleanup.finish(Deadline.after(30))
    assert not prepared.helper_uncertain and prepared.cleaned and len(carrier.requests) == 2


@pytest.mark.parametrize("deadline", [Deadline.after(None), Deadline.after(0)])
def test_closing_requires_finite_live_budget(owned, deadline) -> None:
    prepared = stage(owned)
    with pytest.raises(ValidationError):
        PreparedStageCleanup(prepared).finish(deadline)


def test_control_after_actual_stage_settlement_does_not_create_phantom_custody(owned, monkeypatch) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    cleanup = PreparedStageCleanup(prepared)
    settle = prepared._settle_active
    control = KeyboardInterrupt("settlement returned before control escaped")

    def interrupted(active):
        settle(active)
        assert active.borrow.closed and owner._outstanding_attempt is None
        raise control

    with monkeypatch.context() as patch:
        patch.setattr(prepared, "_settle_active", interrupted)
        with pytest.raises(KeyboardInterrupt) as caught:
            prepared.begin()
    assert caught.value is control and prepared.failed and not prepared.helper_uncertain
    assert prepared._active is None and len(carrier.requests) == 1
    owner.stop_admission()
    assert cleanup.finish(Deadline.after(30)) and pending(owner) == []
    assert len(carrier.requests) == 2


def test_recovered_candidate_survives_interruption_before_debt_capture(owned, monkeypatch) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    prepared.cleanup_debt = None
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()

    def interrupted(deadline, *, observe):
        assert cleanup._active is not None and cleanup._active.candidate is not None
        assert prepared.cleanup_debt is None
        raise KeyboardInterrupt("candidate captured before debt publication")

    with monkeypatch.context() as patch:
        patch.setattr(cleanup, "_settle", interrupted)
        with pytest.raises(KeyboardInterrupt):
            cleanup.finish(Deadline.after(30))
    assert len(carrier.requests) == 2
    assert cleanup.finish(Deadline.after(30)) and pending(owner) == []
    assert [request.operation for request in carrier.requests] == [
        FileStageOperation.BEGIN,
        FileStageOperation.RECONCILE,
        FileStageOperation.CLEANUP,
    ]


@pytest.mark.parametrize("changed", ["generation", "guest", "carrier"])
def test_native_observation_is_fenced_against_changed_binding(owned, changed) -> None:
    database, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    deliveries: list[NativeDelivery] = []

    def factory(expectation):
        delivery = NativeDelivery(carrier, expectation)
        deliveries.append(delivery)
        return delivery

    prepared._new_helper_delivery = factory
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()
    assert not cleanup.finish(Deadline.after(30))
    if changed == "generation":
        OperationOwner.recover(database.operations, owner.ownership, "e" * 32)
    elif changed == "guest":
        prepared._bootstrap = replace(BOOTSTRAP, guest=replace(BOOTSTRAP.guest, init_start_ticks=999))
    else:
        prepared._carrier = ScriptedCarrier(owner)
    with pytest.raises(StateError):
        cleanup.finish(Deadline.after(30))
    assert len(deliveries) == 1 and deliveries[0].observed == [] and not prepared.cleaned


def test_local_cleanup_lost_reply_retries_actual_custody_without_helper_delivery(owned, monkeypatch) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    carrier.unsettled = True
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()
    original = LocalDeliveryCustody.close

    def interrupted(custody, deadline):
        assert original(custody, deadline)
        raise KeyboardInterrupt("local close reply interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(LocalDeliveryCustody, "close", interrupted)
        with pytest.raises(KeyboardInterrupt):
            cleanup.finish(Deadline.after(30))
    assert cleanup._active is not None and cleanup._active.helper.custody is carrier.custody
    assert cleanup.finish(Deadline.after(30)) and len(carrier.requests) == 2
    assert pending(owner) == []


def test_pre_entry_route_control_can_retry_cleanup_without_reopening_admission(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    control = KeyboardInterrupt("closing route refused")

    def refuse(deadline):
        raise control

    prepared._before_dispatch = refuse
    cleanup = PreparedStageCleanup(prepared)
    owner.stop_admission()
    with pytest.raises(KeyboardInterrupt) as caught:
        cleanup.finish(Deadline.after(30))
    assert caught.value is control and cleanup._active is not None and not cleanup._active.helper.entered
    assert len(carrier.requests) == 1 and pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]
    prepared._before_dispatch = None
    assert cleanup.finish(Deadline.after(30)) and len(carrier.requests) == 2
    with pytest.raises(StateError):
        owner.borrow()
