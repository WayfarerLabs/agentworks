"""Real SQLite ownership around scripted numeric stage helper exchanges."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Generator
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _prepared_helper_stage as stage_module
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_stage_exchange import stage_begin
from agentworks.execution._file_stage_protocol import (
    MAX_STAGE_CHUNK_BYTES,
    FileStageBeginRequest,
    FileStageChunkRequest,
    FileStageCleanupRequest,
    FileStageFailureCode,
    FileStageFailureControl,
    FileStageOperation,
    FileStageRequest,
    decode_file_stage_request,
    empty_file_stage_body,
    encode_file_stage_begin_result,
    encode_file_stage_chunk_result,
    encode_file_stage_cleanup_result,
    encode_file_stage_failure,
    encode_file_stage_reconcile_result,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind, encode_file_record
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._prepared_helper_obligation import (
    PREPARED_HELPER_OBLIGATION_KIND,
    decode_prepared_helper_obligation,
)
from agentworks.execution._prepared_helper_stage import PreparedHelperStage
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS, _NumericGuestBootstrap
from agentworks.execution._scratch import ScratchFailureKind, ScratchPhase, ScratchReference
from agentworks.execution._scratch_receipt import (
    ScratchHistoricalOwnership,
    ScratchOperation,
    ScratchOwnership,
    ScratchOwnershipUncertainty,
    ScratchReceiptContext,
    _Identity,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.operations import LifecycleObligation, OperationAttempt, OperationBorrow, OperationOwner

GUEST = VMGuestIdentity("a" * 32, "00000000-0000-4000-8000-000000000001", 123)
ROOT = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.SUDO_ROOT)
BOOTSTRAP = _NumericGuestBootstrap(ROOT, GUEST)
RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
TARGET = ManagedTargetIdentity(ManagedTargetKind.VM, "stage-vm", "v1:" + "b" * 64, vm_guest_boot_id(GUEST))


class Carrier:
    def __init__(self, owner: OperationOwner) -> None:
        self.owner = owner
        self.requests: list[FileStageRequest] = []
        self.reference: ScratchReference | None = None
        self.dispatch = Dispatch.SENT
        self.code: int | None = 0
        self.runtime = "ready"
        self.body_override: bytes | None = None
        self.failure: FileStageFailureControl | None = None
        self.ownership_uncertain = False
        self.unsettled = False
        self.error: BaseException | None = None
        self.before_return: Callable[[], None] | None = None
        self.custody: LocalDeliveryCustody | None = None
        self.deadlines: list[Deadline] = []

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        pass

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        assert custody is not None
        self.custody = custody
        self.deadlines.append(deadline)
        assert isinstance(io.input, FiniteInput) and isinstance(io.output, SinkOutput)
        request = decode_file_stage_request(io.input.data[io.input.data.index(b"{") :])
        self.requests.append(request)
        assert request.root_path == "/tmp" and request.relative_path == "prepared-input" and request.effect_gate is None
        rows = self.owner.list_pending_lifecycle_obligations()
        cleanup = [row for row in rows if row.obligation_kind == PREPARED_HELPER_OBLIGATION_KIND]
        assert len(cleanup) == 1 and cleanup[0].state is LifecycleObligationState.POSSIBLE_EFFECT
        assert decode_prepared_helper_obligation(cleanup[0].payload).token == request.token
        assert any(row.obligation_kind == "carrier-dispatch" for row in rows)
        if self.unsettled:
            custody.begin_process()
        if self.error is not None:
            raise self.error
        if isinstance(request, FileStageBeginRequest):
            self.reference = ScratchReference(
                ScratchOwnership(
                    request.token,
                    ScratchReceiptContext(ScratchOperation.STAGE, ROOT.expected),
                    _Identity(1, 2),
                    _Identity(1, 3),
                    _Identity(1, 4),
                    0,
                    request.expected_length,
                    _Identity(1, 5),
                )
            )
            body = encode_file_stage_begin_result(self.reference)
        elif isinstance(request, FileStageChunkRequest):
            assert hashlib.sha256(request.data).digest() == request.chunk_digest
            body = encode_file_stage_chunk_result()
        elif isinstance(request, FileStageCleanupRequest):
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
        kind = FileRecordKind.RESULT
        if self.failure is not None:
            kind, body = FileRecordKind.FAILED, encode_file_stage_failure(self.failure)
        payload = f"AGW_RUNTIME_1:{request.nonce}:{self.runtime}:0\n".encode() + b"".join(
            encode_file_record(request.nonce, FileRecord(index, kind, data))
            for index, (kind, data) in enumerate(((kind, body), (FileRecordKind.FINISHED, empty_file_stage_body())))
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
def owned(tmp_path: Path) -> Generator[tuple[Database, OperationOwner, Carrier]]:
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, TARGET.name), "test")
    try:
        yield database, owner, Carrier(owner)
    finally:
        database.close()


def stage(owned: tuple[Database, OperationOwner, Carrier], length: int = 3, **kwargs) -> PreparedHelperStage:
    _, owner, carrier = owned
    return PreparedHelperStage(owner, carrier, TARGET, BOOTSTRAP, RUNTIME, Deadline.after(30), length, **kwargs)


def pending(owner: OperationOwner) -> list[str]:
    return [row.obligation_kind for row in owner.list_pending_lifecycle_obligations()]


def test_passive_then_chunked_stage_keeps_separate_cleanup_row(owned) -> None:
    _, owner, carrier = owned
    data = bytes(range(256)) * 100
    prepared = stage(owned, len(data))
    assert carrier.requests == [] and pending(owner) == []
    assert prepared.begin() and pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]
    assert prepared.append(data)
    chunks = [request for request in carrier.requests if isinstance(request, FileStageChunkRequest)]
    assert [request.offset for request in chunks] == [0, MAX_STAGE_CHUNK_BYTES, 2 * MAX_STAGE_CHUNK_BYTES]
    assert b"".join(request.data for request in chunks) == data
    assert all(len(request.data) <= MAX_STAGE_CHUNK_BYTES for request in chunks)
    assert prepared.offset == len(data) and pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]
    assert all(deadline is prepared.deadline for deadline in carrier.deadlines)
    assert prepared.cleanup() and pending(owner) == []
    with pytest.raises(StateError):
        owner.close()


def test_activation_one_shot_and_cleanup_requires_actual_closure(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin() and prepared.append(b"abc")
    request = prepared.prepare_activation("c" * 32, hashlib.sha256(b"abc").digest())
    assert request.reference is prepared.reference and request.remaining_ns > 0
    with pytest.raises(StateError):
        prepared.prepare_activation("c" * 32, request.sha256)
    with pytest.raises(StateError):
        prepared.append(b"")
    before = len(carrier.requests)
    with pytest.raises(StateError):
        prepared.cleanup()
    prepared.note_activation_completion(Dispatch.SENT, ExitStatus(1), LocalDeliveryCustody())
    with pytest.raises(StateError):
        prepared.cleanup()
    assert len(carrier.requests) == before
    prepared.note_activation_completion(Dispatch.NOT_SENT, None, LocalDeliveryCustody())
    assert prepared.cleanup() and pending(owner) == []


@pytest.mark.parametrize("code", [None, 1, 255])
def test_unknown_helper_termination_never_allows_following_effect(owned, code: int | None) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    carrier.code = code
    assert not prepared.begin()
    assert prepared.reference is not None and prepared.cleanup_debt is not None
    assert prepared.helper_uncertain
    for action in (
        lambda: prepared.append(b"abc"),
        prepared.cleanup,
        lambda: prepared.prepare_activation("c" * 32, b"d" * 32),
    ):
        with pytest.raises(StateError):
            action()
    assert len(carrier.requests) == 1 and PREPARED_HELPER_OBLIGATION_KIND in pending(owner)


def test_unknown_local_custody_blocks_even_zero_completion(owned) -> None:
    _, _, carrier = owned
    prepared = stage(owned)
    carrier.unsettled = True
    assert not prepared.begin() and prepared.helper_uncertain
    with pytest.raises(StateError):
        prepared.cleanup()
    assert len(carrier.requests) == 1
    assert carrier.custody is not None
    carrier.custody.close(prepared.deadline)


@pytest.mark.parametrize("dispatch", [Dispatch.NOT_SENT, Dispatch.UNKNOWN])
def test_non_sent_chunk_never_advances_offset_or_replays(owned, dispatch: Dispatch) -> None:
    _, _, carrier = owned
    prepared = stage(owned, MAX_STAGE_CHUNK_BYTES + 1)
    assert prepared.begin()
    carrier.dispatch = dispatch
    assert not prepared.append(b"a" * (MAX_STAGE_CHUNK_BYTES + 1))
    assert prepared.offset == 0 and len(carrier.requests) == 2
    with pytest.raises(StateError):
        prepared.append(b"a")


def test_invalid_chunk_result_stops_transfer_but_closed_helper_can_cleanup(owned) -> None:
    _, _, carrier = owned
    prepared = stage(owned, MAX_STAGE_CHUNK_BYTES + 1)
    assert prepared.begin()
    carrier.body_override = b"{}"
    assert not prepared.append(b"a" * (MAX_STAGE_CHUNK_BYTES + 1))
    assert prepared.offset == 0 and not prepared.helper_uncertain
    carrier.body_override = None
    assert prepared.cleanup()


def test_partial_begin_reconciles_only_cleanup_not_activation(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    carrier.body_override = b"{}"
    assert not prepared.begin() and prepared.reference is None
    carrier.body_override = None
    assert prepared.cleanup()
    assert [request.operation for request in carrier.requests] == [
        FileStageOperation.BEGIN,
        FileStageOperation.RECONCILE,
        FileStageOperation.CLEANUP,
    ]
    assert prepared.reference is None and pending(owner) == []
    with pytest.raises(StateError):
        prepared.prepare_activation("c" * 32, b"d" * 32)


def test_token_reconciliation_uncertainty_retains_cleanup_row(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    carrier.body_override = b"{}"
    assert not prepared.begin()
    carrier.body_override = None
    carrier.ownership_uncertain = True
    assert not prepared.cleanup()
    assert prepared.reference is None and prepared.cleanup_debt is None
    assert pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]
    assert len(carrier.requests) == 2


def test_reference_captured_before_borrow_close_failure(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    original = OperationBorrow.close
    interrupted = KeyboardInterrupt("close reply lost")

    def close(borrow: OperationBorrow) -> None:
        assert prepared.reference == carrier.reference and prepared.cleanup_debt is not None
        original(borrow)
        raise interrupted

    monkeypatch.setattr(OperationBorrow, "close", close)
    with pytest.raises(KeyboardInterrupt) as caught:
        prepared.begin()
    assert caught.value is interrupted and prepared.helper_uncertain
    assert pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]
    with pytest.raises(StateError):
        prepared.cleanup()


@pytest.mark.parametrize("arm", [False, True])
def test_registration_and_arm_lost_reply_never_dispatch(owned, monkeypatch: pytest.MonkeyPatch, arm: bool) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    interrupted = KeyboardInterrupt("admission reply lost")
    if arm:
        original_arm = LifecycleObligation.mark_possible_effect

        def mark(obligation: LifecycleObligation) -> None:
            original_arm(obligation)
            raise interrupted

        monkeypatch.setattr(LifecycleObligation, "mark_possible_effect", mark)
    else:
        original_register = owner.register_lifecycle_obligation

        def register(*args, **kwargs):
            original_register(*args, **kwargs)
            raise interrupted

        monkeypatch.setattr(owner, "register_lifecycle_obligation", register)
    with pytest.raises(KeyboardInterrupt) as caught:
        prepared.begin()
    assert caught.value is interrupted and prepared.registration_uncertain
    assert carrier.requests == [] and pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]
    for action in (prepared.begin, prepared.cleanup):
        with pytest.raises(StateError):
            action()


def test_no_payload_retention_and_only_own_cleanup_resolved(owned) -> None:
    _, owner, carrier = owned
    other = owner.register_lifecycle_obligation("other", payload_version=1, payload=b"{}")
    other.mark_possible_effect()
    prepared = stage(owned)
    assert prepared.begin() and prepared.append(b"abc") and prepared.cleanup()
    assert pending(owner) == ["other"]
    assert all(value != b"abc" for value in vars(prepared).values())
    for row in owner.list_pending_lifecycle_obligations():
        assert b"abc" not in row.payload
    assert prepared.token.hex() not in repr(prepared)


def test_length_and_activation_completeness(owned) -> None:
    _, _, carrier = owned
    prepared = stage(owned)
    assert prepared.begin() and prepared.append(b"a")
    with pytest.raises(StateError):
        prepared.prepare_activation("c" * 32, b"d" * 32)
    with pytest.raises(ValidationError):
        prepared.append(b"too much")
    assert len(carrier.requests) == 2 and prepared.offset == 1
    assert prepared.append(b"bc") and prepared.cleanup()


def test_constructor_refuses_unbounded_expired_and_wrong_owner(owned) -> None:
    _, owner, carrier = owned
    for deadline in (Deadline.after(None), Deadline.after(0)):
        with pytest.raises(ValidationError):
            PreparedHelperStage(owner, carrier, TARGET, BOOTSTRAP, RUNTIME, deadline, 3)
    with pytest.raises(ValidationError):
        PreparedHelperStage(owner, carrier, replace(TARGET, name="other-vm"), BOOTSTRAP, RUNTIME, Deadline.after(30), 3)
    for length in (-1, 2**63):
        with pytest.raises(ValidationError):
            PreparedHelperStage(owner, carrier, TARGET, BOOTSTRAP, RUNTIME, Deadline.after(30), length)
    assert carrier.requests == [] and pending(owner) == []


def test_expiry_never_starts_cleanup_or_activation(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin() and prepared.append(b"abc")
    monkeypatch.setattr(Deadline, "remaining", lambda deadline: 0.0)
    for action in (prepared.cleanup, lambda: prepared.prepare_activation("c" * 32, b"d" * 32)):
        with pytest.raises(StateError):
            action()
    assert len(carrier.requests) == 2 and pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]


def test_resolution_lost_reply_cannot_reuse_already_cleaned_stage(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    interrupted = KeyboardInterrupt("resolution reply lost")
    original = LifecycleObligation.resolve

    def resolve(obligation: LifecycleObligation) -> None:
        if obligation.obligation_id == prepared.obligation_id:
            original(obligation)
            raise interrupted
        original(obligation)

    monkeypatch.setattr(LifecycleObligation, "resolve", resolve)
    with pytest.raises(KeyboardInterrupt) as caught:
        prepared.cleanup()
    assert caught.value is interrupted
    assert prepared.cleaned and prepared.cleanup_resolution_uncertain and pending(owner) == []
    with pytest.raises(StateError):
        prepared.cleanup()
    with pytest.raises(StateError):
        prepared.append(b"abc")
    assert len(carrier.requests) == 2


def test_activation_unsettled_local_delivery_cannot_release_stage(owned) -> None:
    _, _, carrier = owned
    prepared = stage(owned, 0)
    assert prepared.begin()
    prepared.prepare_activation("c" * 32, hashlib.sha256(b"").digest())
    delivery = LocalDeliveryCustody()
    delivery.begin_process()
    prepared.note_activation_completion(Dispatch.SENT, ExitStatus(0), delivery)
    with pytest.raises(StateError):
        prepared.cleanup()
    assert len(carrier.requests) == 1
    assert delivery.close(prepared.deadline)
    prepared.note_activation_completion(Dispatch.SENT, ExitStatus(0), delivery)
    assert prepared.cleanup()


def test_settlement_lost_reply_captures_ownership_but_forbids_later_dispatch(
    owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, carrier = owned
    prepared = stage(owned)
    original = OperationAttempt.settle
    interrupted = KeyboardInterrupt("attempt settlement lost")

    def settle(attempt: OperationAttempt) -> None:
        assert prepared.reference is not None and prepared.cleanup_debt is not None
        original(attempt)
        raise interrupted

    monkeypatch.setattr(OperationAttempt, "settle", settle)
    with pytest.raises(KeyboardInterrupt) as caught:
        prepared.begin()
    assert caught.value is interrupted and prepared.helper_uncertain
    assert len(carrier.requests) == 1
    with pytest.raises(StateError):
        prepared.cleanup()


def test_original_helper_control_is_not_stored_or_rewritten(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    interrupted = KeyboardInterrupt("helper interrupted")
    carrier.error = interrupted
    with pytest.raises(KeyboardInterrupt) as caught:
        prepared.begin()
    assert caught.value is interrupted and prepared.helper_uncertain
    assert all(value is not interrupted for value in vars(prepared).values())
    assert PREPARED_HELPER_OBLIGATION_KIND in pending(owner)


def test_route_refusal_keeps_cleanup_admission_without_dispatch(owned) -> None:
    _, owner, carrier = owned
    interrupted = KeyboardInterrupt("route changed")

    def route() -> None:
        assert pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]
        raise interrupted

    prepared = stage(owned, before_dispatch=route)
    with pytest.raises(KeyboardInterrupt) as caught:
        prepared.begin()
    assert caught.value is interrupted and prepared.helper_uncertain
    assert carrier.requests == []


def test_partial_append_preserves_only_acknowledged_offset(owned) -> None:
    _, _, carrier = owned
    prepared = stage(owned, 2 * MAX_STAGE_CHUNK_BYTES + 1)
    assert prepared.begin()

    def next_result() -> None:
        if len(carrier.requests) == 2:
            carrier.body_override = b"{}"

    carrier.before_return = next_result
    assert not prepared.append(b"a" * prepared.expected_length)
    assert prepared.offset == MAX_STAGE_CHUNK_BYTES and len(carrier.requests) == 3
    carrier.before_return = None
    carrier.body_override = None
    assert prepared.cleanup()


def test_runtime_refusal_never_advances_chunk(owned) -> None:
    _, _, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    carrier.runtime = "missing"
    assert not prepared.append(b"abc")
    assert prepared.offset == 0 and not prepared.helper_uncertain
    carrier.runtime = "ready"
    assert prepared.cleanup()


def test_activation_requires_positive_floored_nanoseconds(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.execution._staged_helper_protocol import StagedHelperError

    _, _, carrier = owned
    prepared = stage(owned, 0)
    assert prepared.begin()
    monkeypatch.setattr(Deadline, "remaining", lambda deadline: 0.0000000001)
    with pytest.raises(StagedHelperError):
        prepared.prepare_activation("c" * 32, b"d" * 32)
    assert len(carrier.requests) == 1


def test_begin_not_sent_does_not_adopt_contradictory_reference_and_resolves_only_stage(
    owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner, carrier = owned
    other = owner.register_lifecycle_obligation("other", payload_version=1, payload=b"{}")
    other.mark_possible_effect()
    prepared = stage(owned)
    original = stage_begin

    def contradictory(*args, **kwargs):
        return replace(original(*args, **kwargs), dispatch=Dispatch.NOT_SENT)

    monkeypatch.setattr(stage_module, "stage_begin", contradictory)
    assert not prepared.begin()
    assert prepared.reference is None and prepared.cleanup_debt is None and prepared.cleaned
    assert pending(owner) == ["other"] and len(carrier.requests) == 1
    with pytest.raises(StateError):
        prepared.cleanup()


def test_partial_cleanup_cannot_reactivate_or_append(owned) -> None:
    _, _, carrier = owned
    prepared = stage(owned)
    assert prepared.begin() and prepared.append(b"abc")
    carrier.body_override = b"{}"
    assert not prepared.cleanup()
    for action in (lambda: prepared.append(b""), lambda: prepared.prepare_activation("c" * 32, b"d" * 32)):
        with pytest.raises(StateError):
            action()
    carrier.body_override = None
    assert prepared.cleanup()


@pytest.mark.parametrize("code", [code for code in FileStageFailureCode if code is not FileStageFailureCode.SCRATCH])
def test_trusted_begin_refusal_before_creation_resolves_stage_only(owned, code: FileStageFailureCode) -> None:
    _, owner, carrier = owned
    other = owner.register_lifecycle_obligation("other", payload_version=1, payload=b"{}")
    other.mark_possible_effect()
    prepared = stage(owned)
    carrier.failure = FileStageFailureControl(code)
    assert not prepared.begin() and prepared.cleaned and prepared.reference is None
    assert pending(owner) == ["other"] and len(carrier.requests) == 1


def test_scratch_begin_failure_without_debt_retains_cleanup_uncertainty(owned) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    carrier.failure = FileStageFailureControl(FileStageFailureCode.SCRATCH, ScratchFailureKind.IO, ScratchPhase.BEGIN)
    assert not prepared.begin() and not prepared.cleaned and prepared.cleanup_debt is None
    assert pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]
    carrier.failure = None
    carrier.ownership_uncertain = True
    assert not prepared.cleanup() and not prepared.cleaned
    assert pending(owner) == [PREPARED_HELPER_OBLIGATION_KIND]


def test_explicit_fresh_cleanup_budget_does_not_reopen_original_authority(
    owned, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, owner, carrier = owned
    prepared = stage(owned)
    assert prepared.begin() and prepared.append(b"abc")
    original = prepared.deadline
    monkeypatch.setattr(prepared, "_deadline", Deadline.after(0))
    for action in (
        prepared.cleanup,
        lambda: prepared.append(b""),
        lambda: prepared.prepare_activation("c" * 32, b"d" * 32),
    ):
        with pytest.raises(StateError):
            action()
    with pytest.raises(ValidationError):
        prepared.cleanup(deadline=Deadline.after(None))
    fresh = Deadline.after(30)
    assert prepared.cleanup(deadline=fresh)
    assert carrier.deadlines == [original, original, fresh] and pending(owner) == []


@pytest.mark.parametrize("activation", [False, True])
def test_fresh_cleanup_budget_cannot_infer_helper_or_activation_closure(
    owned, monkeypatch: pytest.MonkeyPatch, activation: bool
) -> None:
    _, _, carrier = owned
    prepared = stage(owned, 0)
    if activation:
        assert prepared.begin()
        prepared.prepare_activation("c" * 32, b"d" * 32)
    else:
        carrier.unsettled = True
        assert not prepared.begin()
    monkeypatch.setattr(prepared, "_deadline", Deadline.after(0))
    fresh = Deadline.after(30)
    with pytest.raises(StateError):
        prepared.cleanup(deadline=fresh)
    assert len(carrier.requests) == 1
    if carrier.custody is not None:
        carrier.custody.close(fresh)


def test_interrupt_after_accepted_chunk_cannot_replay_range(owned, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, carrier = owned
    prepared = stage(owned)
    assert prepared.begin()
    original = prepared._call
    interrupted = KeyboardInterrupt("offset publication interrupted")

    def after_return(action, **kwargs):
        original(action, **kwargs)
        raise interrupted

    monkeypatch.setattr(prepared, "_call", after_return)
    with pytest.raises(KeyboardInterrupt) as caught:
        prepared.append(b"abc")
    assert caught.value is interrupted and prepared.failed and prepared.offset == 0
    with pytest.raises(StateError):
        prepared.append(b"abc")
    assert len(carrier.requests) == 2
    monkeypatch.setattr(prepared, "_call", original)
    assert prepared.cleanup()
