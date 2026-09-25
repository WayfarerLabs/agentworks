"""Host reducer and one-attempt behavior for private stage exchanges."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._file_stage_bundle import FIXED_BUNDLE
from agentworks.execution._file_stage_exchange import (
    FileStageChunkUncertain,
    FileStageCleanupUncertain,
    FileStageCreationUncertain,
    FileStageObservationError,
    FileStageObservationState,
    FileStageReconcileUncertain,
    stage_begin,
    stage_chunk,
    stage_cleanup,
    stage_reconcile,
)
from agentworks.execution._file_stage_protocol import (
    FileStageBeginRequest,
    FileStageChunkRequest,
    FileStageCleanupRequest,
    FileStageFailureCode,
    FileStageFailureControl,
    FileStageReconcileRequest,
    FileStageRequest,
    decode_file_stage_request,
    empty_file_stage_body,
    encode_file_stage_begin_result,
    encode_file_stage_chunk_result,
    encode_file_stage_cleanup_result,
    encode_file_stage_failure,
    encode_file_stage_reconcile_result,
    stage_context,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileWireError, encode_file_record
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._scratch import ScratchFailureKind, ScratchPhase, ScratchReference
from agentworks.execution._scratch_receipt import (
    _RECEIPT_BUILD_MODE,
    _RECEIPT_MODE,
    ScratchCleanupDebt,
    ScratchHistoricalOwnership,
    ScratchOwnership,
    ScratchOwnershipUncertainty,
    _Identity,
    scratch_name,
)
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from tests.execution.files._runtime_support import (
    runtime_ready_record,
    runtime_selection,
)

_TOKEN = bytes(range(16))


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)


def _reference(request: FileStageRequest, *, length: int | None = None) -> ScratchReference:
    if length is None:
        length = request.expected_length if isinstance(request, FileStageBeginRequest) else 20_000
    return ScratchReference(
        ScratchOwnership(
            request.token,
            stage_context(request.identity),
            _Identity(1, 2),
            _Identity(1, 3),
            _Identity(1, 4),
            1002,
            length,
            _Identity(1, 5),
        )
    )


def _debt(request: FileStageRequest) -> ScratchCleanupDebt:
    return ScratchCleanupDebt(
        scratch_name(request.token),
        _Identity(1, 2),
        _Identity(1, 3),
        _Identity(1, 4),
        _Identity(1, 5),
        (_RECEIPT_MODE,),
        request.identity.euid,
        1002,
    )


def _write(sink: object, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = sink.try_write(remaining)  # type: ignore[attr-defined]
        assert written is not None and written > 0
        remaining = remaining[written:]


@dataclass
class TranscriptCarrier:
    build: object
    dispatch: Dispatch = Dispatch.SENT
    stderr: bytes = b""
    complete: bool = True
    failure: Failure | None = None
    calls: int = 0
    invocation: PreparedInvocation | None = None
    io: CarrierIO | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        del invocation, io

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.validate(invocation, io=io)
        del deadline
        self.calls += 1
        self.invocation = invocation
        self.io = io
        assert isinstance(io.input, FiniteInput)
        assert io.input.sensitive and io.sensitive and isinstance(io.output, SinkOutput)
        assert io.input.data.startswith(FIXED_BUNDLE.prefix)
        request = decode_file_stage_request(io.input.data[len(FIXED_BUNDLE.prefix) :])
        transcript = self.build(request)  # type: ignore[operator]
        _write(io.output.stdout, runtime_ready_record(invocation) + transcript)
        _write(io.output.stderr, self.stderr)
        output = CapturedOutput(complete=self.complete, retention=Retention.DELIVERED)
        return CarrierReport(
            self.dispatch,
            ExitStatus(code=29) if self.dispatch is Dispatch.SENT else None,
            29,
            output,
            output,
            self.failure,
        )


def _records(request: FileStageRequest, kind: FileRecordKind, body: bytes) -> bytes:
    return encode_file_record(request.nonce, FileRecord(0, kind, body)) + encode_file_record(
        request.nonce,
        FileRecord(1, FileRecordKind.FINISHED, empty_file_stage_body()),
    )


def _begin_records(request: FileStageRequest) -> bytes:
    assert isinstance(request, FileStageBeginRequest)
    return _records(
        request,
        FileRecordKind.RESULT,
        encode_file_stage_begin_result(_reference(request)),
    )


def _chunk_records(request: FileStageRequest) -> bytes:
    assert isinstance(request, FileStageChunkRequest)
    return _records(request, FileRecordKind.RESULT, encode_file_stage_chunk_result())


def _reconcile_records(request: FileStageRequest) -> bytes:
    assert isinstance(request, FileStageReconcileRequest)
    historical = ScratchHistoricalOwnership(_reference(request)._ownership)
    return _records(request, FileRecordKind.RESULT, encode_file_stage_reconcile_result(historical))


def _cleanup_records(request: FileStageRequest) -> bytes:
    assert isinstance(request, FileStageCleanupRequest)
    return _records(request, FileRecordKind.RESULT, encode_file_stage_cleanup_result())


def test_complete_begin_exposes_reference_only_after_sensitive_terminal_exchange(plan: IdentityPlan) -> None:
    carrier = TranscriptCarrier(_begin_records)

    result = stage_begin(
        carrier,
        trusted_root_path="/trusted/root-canary",
        relative_path="nested/target-canary",
        token=_TOKEN,
        expected_length=20_000,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    assert carrier.calls == 1
    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.CREATED
    result_reference = result_observation.reference
    assert result_reference is not None
    assert result_reference._ownership._token == _TOKEN
    assert result.carrier_completion == ExitStatus(code=29)
    assert carrier.io is not None and isinstance(carrier.io.input, FiniteInput)
    assert carrier.io.input.sensitive and carrier.io.input.data.isascii()
    carrier_invocation = carrier.invocation
    assert carrier_invocation is not None
    assert "root-canary" not in carrier_invocation.argv
    assert "target-canary" not in carrier_invocation.argv
    assert "root-canary" not in repr(result) and "target-canary" not in repr(result)


def test_complete_begin_with_different_declared_length_is_uncertain_control(plan: IdentityPlan) -> None:
    def mismatched_length(request: FileStageRequest) -> bytes:
        assert isinstance(request, FileStageBeginRequest)
        return _records(
            request,
            FileRecordKind.RESULT,
            encode_file_stage_begin_result(_reference(request, length=request.expected_length + 1)),
        )

    result = stage_begin(
        TranscriptCarrier(mismatched_length),
        trusted_root_path="/trusted/root",
        relative_path="target",
        token=_TOKEN,
        expected_length=20_000,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.UNCERTAIN
    assert result_observation.error is FileStageObservationError.CONTROL
    assert result_observation.reference is None


def test_complete_chunk_is_accepted_once_without_payload_retention(plan: IdentityPlan) -> None:
    payload = b"payload-secret-canary"
    begin_request = FileStageBeginRequest(
        "0" * 32,
        "/trusted/root",
        "nested/target",
        _TOKEN,
        20_000,
        plan.expected,
        1.0,
    )
    carrier = TranscriptCarrier(_chunk_records)

    result = stage_chunk(
        carrier,
        trusted_root_path=begin_request.root_path,
        relative_path=begin_request.relative_path,
        token=_TOKEN,
        reference=_reference(begin_request),
        offset=0,
        data=payload,
        chunk_digest=hashlib.sha256(payload).digest(),
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    assert carrier.calls == 1
    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.ACCEPTED
    assert payload.decode() not in repr(result)
    assert carrier.invocation is not None and payload.decode() not in carrier.invocation.argv


def test_complete_reconcile_exposes_only_cleanup_debt_after_terminal(plan: IdentityPlan) -> None:
    carrier = TranscriptCarrier(_reconcile_records)

    result = stage_reconcile(
        carrier,
        trusted_root_path="/trusted/root-canary",
        relative_path="nested/target-canary",
        token=_TOKEN,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.RECOVERED
    result_cleanup_debt = result_observation.cleanup_debt
    assert result_cleanup_debt is not None
    assert result_observation.reference is None
    assert carrier.io is not None and carrier.io.sensitive
    carrier_invocation = carrier.invocation
    assert carrier_invocation is not None
    assert "root-canary" not in carrier_invocation.argv
    assert "target-canary" not in carrier_invocation.argv


def test_truncated_reconcile_never_exposes_parsed_cleanup_debt(plan: IdentityPlan) -> None:
    result = stage_reconcile(
        TranscriptCarrier(lambda request: _reconcile_records(request)[:-1]),
        trusted_root_path="/trusted/root",
        relative_path="target",
        token=_TOKEN,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.UNCERTAIN
    assert result_observation.cleanup_debt is None
    assert result_observation.error is FileWireError.TRUNCATED


def test_complete_reconcile_can_report_ownership_uncertainty(plan: IdentityPlan) -> None:
    def uncertain(request: FileStageRequest) -> bytes:
        assert isinstance(request, FileStageReconcileRequest)
        return _records(
            request,
            FileRecordKind.RESULT,
            encode_file_stage_reconcile_result(ScratchOwnershipUncertainty()),
        )

    result = stage_reconcile(
        TranscriptCarrier(uncertain),
        trusted_root_path="/trusted/root",
        relative_path="target",
        token=_TOKEN,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.OWNERSHIP_UNCERTAIN
    assert result_observation.cleanup_debt is None


def _reconcile_refusal(
    plan: IdentityPlan,
    debt_change: Callable[[ScratchCleanupDebt], ScratchCleanupDebt | None],
):
    def refusal(request: FileStageRequest) -> bytes:
        assert isinstance(request, FileStageReconcileRequest)
        failure = FileStageFailureControl(
            FileStageFailureCode.SCRATCH,
            ScratchFailureKind.DEADLINE,
            ScratchPhase.RECONCILE,
            debt_change(_debt(request)),
        )
        return _records(request, FileRecordKind.FAILED, encode_file_stage_failure(failure))

    return stage_reconcile(
        TranscriptCarrier(refusal),
        trusted_root_path="/trusted/root",
        relative_path="target",
        token=_TOKEN,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )


def test_reconcile_deadline_refusal_accepts_complete_historical_debt(plan: IdentityPlan) -> None:
    result = _reconcile_refusal(plan, lambda debt: debt)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_failure.cleanup_debt is not None


@pytest.mark.parametrize(
    "debt_change",
    [
        lambda debt: replace(debt, _parent=None),
        lambda debt: replace(debt, _directory=None),
        lambda debt: replace(debt, _object=None),
        lambda debt: replace(debt, _receipt=None),
        lambda debt: replace(debt, _receipt_modes=(_RECEIPT_BUILD_MODE, _RECEIPT_MODE)),
    ],
    ids=["parent", "directory", "data", "receipt", "receipt-mode"],
)
def test_reconcile_deadline_refusal_rejects_impossible_historical_debt(
    plan: IdentityPlan,
    debt_change: Callable[[ScratchCleanupDebt], ScratchCleanupDebt | None],
) -> None:
    result = _reconcile_refusal(plan, debt_change)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.UNCERTAIN
    assert result_observation.error is FileStageObservationError.CONTROL
    assert result_observation.failure is None


def test_reconcile_reply_from_another_request_nonce_is_uncertain(plan: IdentityPlan) -> None:
    def crossbound(request: FileStageRequest) -> bytes:
        assert isinstance(request, FileStageReconcileRequest)
        historical = ScratchHistoricalOwnership(_reference(request)._ownership)
        return encode_file_record(
            "0" * 32,
            FileRecord(0, FileRecordKind.RESULT, encode_file_stage_reconcile_result(historical)),
        )

    result = stage_reconcile(
        TranscriptCarrier(crossbound),
        trusted_root_path="/trusted/root",
        relative_path="target",
        token=_TOKEN,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.UNCERTAIN
    assert result_observation.error is FileWireError.NONCE
    assert result_observation.cleanup_debt is None


def test_complete_cleanup_reports_only_attempt_observation(plan: IdentityPlan) -> None:
    begin = FileStageBeginRequest("0" * 32, "/trusted/root", "target", _TOKEN, 1, plan.expected, 1.0)
    result = stage_cleanup(
        TranscriptCarrier(_cleanup_records),
        trusted_root_path=begin.root_path,
        relative_path=begin.relative_path,
        token=_TOKEN,
        cleanup_debt=_debt(begin),
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.CLEANED
    assert result_observation.cleanup_debt is None


@pytest.mark.parametrize(
    ("dispatch", "expected"),
    [
        (Dispatch.NOT_SENT, FileStageObservationState.INCOMPLETE),
        (Dispatch.SENT, FileStageObservationState.UNCERTAIN),
        (Dispatch.UNKNOWN, FileStageObservationState.UNCERTAIN),
    ],
)
def test_lost_creation_acknowledgement_is_never_replayed_or_reported_absent(
    plan: IdentityPlan,
    dispatch: Dispatch,
    expected: FileStageObservationState,
) -> None:
    carrier = TranscriptCarrier(lambda _request: b"", dispatch=dispatch)

    result = stage_begin(
        carrier,
        trusted_root_path="/trusted/root",
        relative_path="target",
        token=_TOKEN,
        expected_length=1,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    assert carrier.calls == 1
    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is expected
    assert result_observation.error is FileStageObservationError.MISSING_TERMINAL
    assert result_observation.reference is None


def test_complete_refusal_exposes_known_cleanup_debt(plan: IdentityPlan) -> None:
    def refusal(request: FileStageRequest) -> bytes:
        failure = FileStageFailureControl(
            FileStageFailureCode.SCRATCH,
            ScratchFailureKind.IO,
            ScratchPhase.BEGIN,
            _debt(request),
        )
        return _records(
            request,
            FileRecordKind.FAILED,
            encode_file_stage_failure(failure),
        )

    result = stage_begin(
        TranscriptCarrier(refusal),
        trusted_root_path="/trusted/root",
        relative_path="target",
        token=_TOKEN,
        expected_length=1,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_failure.cleanup_debt is not None
    assert result_observation.reference is None


def test_truncated_refusal_never_exposes_parsed_cleanup_debt(plan: IdentityPlan) -> None:
    def refusal(request: FileStageRequest) -> bytes:
        failure = FileStageFailureControl(
            FileStageFailureCode.SCRATCH,
            ScratchFailureKind.IO,
            ScratchPhase.BEGIN,
            _debt(request),
        )
        return _records(
            request,
            FileRecordKind.FAILED,
            encode_file_stage_failure(failure),
        )[:-1]

    result = stage_begin(
        TranscriptCarrier(refusal),
        trusted_root_path="/trusted/root",
        relative_path="target",
        token=_TOKEN,
        expected_length=1,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.UNCERTAIN
    assert result_observation.failure is None
    assert result_observation.error is FileWireError.TRUNCATED


def _chunk_refusal(
    plan: IdentityPlan,
    debt_change: Callable[[ScratchCleanupDebt], ScratchCleanupDebt | None],
    *,
    code: FileStageFailureCode = FileStageFailureCode.SCRATCH,
):
    begin_request = FileStageBeginRequest(
        "0" * 32,
        "/trusted/root",
        "nested/target",
        _TOKEN,
        20_000,
        plan.expected,
        1.0,
    )
    reference = _reference(begin_request)

    def refusal(request: FileStageRequest) -> bytes:
        failure = (
            FileStageFailureControl(code)
            if code is not FileStageFailureCode.SCRATCH
            else FileStageFailureControl(
                code,
                ScratchFailureKind.IO,
                ScratchPhase.WRITE,
                debt_change(_debt(request)),
            )
        )
        return _records(request, FileRecordKind.FAILED, encode_file_stage_failure(failure))

    data = b"chunk"
    return stage_chunk(
        TranscriptCarrier(refusal),
        trusted_root_path=begin_request.root_path,
        relative_path=begin_request.relative_path,
        token=_TOKEN,
        reference=reference,
        offset=0,
        data=data,
        chunk_digest=hashlib.sha256(data).digest(),
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )


def test_chunk_scratch_refusal_exposes_only_matching_reference_debt(plan: IdentityPlan) -> None:
    result = _chunk_refusal(plan, lambda debt: debt)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_failure.cleanup_debt is not None


@pytest.mark.parametrize(
    "debt_change",
    [
        lambda _debt: None,
        lambda debt: replace(debt, _parent=_Identity(9, 2)),
        lambda debt: replace(debt, _directory=_Identity(9, 3)),
        lambda debt: replace(debt, _object=_Identity(9, 4)),
        lambda debt: replace(debt, _receipt=_Identity(9, 5)),
        lambda debt: replace(debt, _receipt_modes=(_RECEIPT_BUILD_MODE, _RECEIPT_MODE)),
        lambda debt: replace(debt, _gid=9999),
    ],
    ids=["absent", "parent", "directory", "data", "receipt", "receipt-mode", "gid"],
)
def test_chunk_scratch_refusal_rejects_debt_not_equal_to_request_reference(
    plan: IdentityPlan,
    debt_change: Callable[[ScratchCleanupDebt], ScratchCleanupDebt | None],
) -> None:
    result = _chunk_refusal(plan, debt_change)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.UNCERTAIN
    assert result_observation.error is FileStageObservationError.CONTROL
    assert result_observation.failure is None


def test_chunk_prescratch_refusal_accepts_non_scratch_failure_without_debt(plan: IdentityPlan) -> None:
    result = _chunk_refusal(plan, lambda _debt: None, code=FileStageFailureCode.ROOT_REFUSED)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_failure.code is FileStageFailureCode.ROOT_REFUSED
    assert result_failure.cleanup_debt is None


def _cleanup_refusal(
    plan: IdentityPlan,
    debt_change: Callable[[ScratchCleanupDebt], ScratchCleanupDebt | None],
):
    begin = FileStageBeginRequest("0" * 32, "/trusted/root", "target", _TOKEN, 1, plan.expected, 1.0)
    debt = _debt(begin)

    def refusal(request: FileStageRequest) -> bytes:
        assert isinstance(request, FileStageCleanupRequest)
        failure = FileStageFailureControl(
            FileStageFailureCode.SCRATCH,
            ScratchFailureKind.CONFLICT,
            ScratchPhase.CLEANUP,
            debt_change(debt),
        )
        return _records(request, FileRecordKind.FAILED, encode_file_stage_failure(failure))

    return stage_cleanup(
        TranscriptCarrier(refusal),
        trusted_root_path=begin.root_path,
        relative_path=begin.relative_path,
        token=_TOKEN,
        cleanup_debt=debt,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )


def test_cleanup_scratch_refusal_exposes_only_matching_request_debt(plan: IdentityPlan) -> None:
    result = _cleanup_refusal(plan, lambda debt: debt)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_failure.cleanup_debt is not None


@pytest.mark.parametrize(
    "debt_change",
    [
        lambda _debt: None,
        lambda debt: replace(debt, _parent=_Identity(9, 2)),
        lambda debt: replace(debt, _directory=_Identity(9, 3)),
        lambda debt: replace(debt, _object=_Identity(9, 4)),
        lambda debt: replace(debt, _receipt=_Identity(9, 5)),
        lambda debt: replace(debt, _receipt_modes=(_RECEIPT_BUILD_MODE, _RECEIPT_MODE)),
        lambda debt: replace(debt, _gid=9999),
    ],
    ids=["absent", "parent", "directory", "data", "receipt", "receipt-mode", "gid"],
)
def test_cleanup_scratch_refusal_rejects_debt_not_equal_to_request(
    plan: IdentityPlan,
    debt_change: Callable[[ScratchCleanupDebt], ScratchCleanupDebt | None],
) -> None:
    result = _cleanup_refusal(plan, debt_change)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.UNCERTAIN
    assert result_observation.error is FileStageObservationError.CONTROL
    assert result_observation.failure is None


@pytest.mark.parametrize(
    ("build", "stderr", "error"),
    [
        (lambda request: b"noise\n" + _begin_records(request), b"", FileWireError.MALFORMED),
        (
            lambda request: encode_file_record(
                "0" * 32,
                FileRecord(0, FileRecordKind.RESULT, b"{}"),
            ),
            b"",
            FileWireError.NONCE,
        ),
        (
            lambda request: (
                _begin_records(request)
                + encode_file_record(
                    request.nonce,
                    FileRecord(
                        2,
                        FileRecordKind.RESULT,
                        encode_file_stage_begin_result(_reference(request)),
                    ),
                )
            ),
            b"",
            FileStageObservationError.POST_TERMINAL,
        ),
        (lambda request: _begin_records(request), b"foreign-stderr-canary", FileStageObservationError.STDERR),
    ],
)
def test_noisy_wrong_nonce_duplicate_and_stderr_creation_responses_are_uncertain(
    plan: IdentityPlan,
    build,
    stderr: bytes,
    error: object,
) -> None:
    result = stage_begin(
        TranscriptCarrier(build, stderr=stderr),
        trusted_root_path="/trusted/root",
        relative_path="target",
        token=_TOKEN,
        expected_length=1,
        plan=plan,
        deadline=Deadline.after(1),
        runtime_selection=runtime_selection(),
    )

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileStageObservationState.UNCERTAIN
    assert result_observation.error is error
    assert "foreign-stderr-canary" not in repr(result)


@pytest.mark.parametrize(
    ("operation", "cause_type"),
    [
        ("begin", FileStageCreationUncertain),
        ("chunk", FileStageChunkUncertain),
        ("reconcile", FileStageReconcileUncertain),
        ("cleanup", FileStageCleanupUncertain),
    ],
)
def test_control_interruption_propagates_with_operation_specific_uncertainty(
    plan: IdentityPlan,
    operation: str,
    cause_type: type[Exception],
) -> None:
    class InterruptingCarrier:
        @property
        def features(self) -> ChannelFeatures:
            return ChannelFeatures()

        def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
            del invocation, io

        def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
            self.validate(invocation, io=io)
            del invocation, io, deadline
            raise KeyboardInterrupt

    kwargs = {
        "carrier": InterruptingCarrier(),
        "trusted_root_path": "/trusted/root",
        "relative_path": "target",
        "token": _TOKEN,
        "plan": plan,
        "deadline": Deadline.after(1),
        "runtime_selection": runtime_selection(),
    }
    with pytest.raises(KeyboardInterrupt) as raised:
        if operation == "begin":
            stage_begin(**kwargs, expected_length=1)
        elif operation == "chunk":
            request = FileStageBeginRequest("0" * 32, "/trusted/root", "target", _TOKEN, 1, plan.expected, 1.0)
            data = b"x"
            stage_chunk(
                **kwargs,
                reference=_reference(request),
                offset=0,
                data=data,
                chunk_digest=hashlib.sha256(data).digest(),
            )
        elif operation == "reconcile":
            stage_reconcile(**kwargs)
        else:
            request = FileStageBeginRequest("0" * 32, "/trusted/root", "target", _TOKEN, 1, plan.expected, 1.0)
            stage_cleanup(**kwargs, cleanup_debt=_debt(request))
    assert isinstance(raised.value.__cause__, cause_type)


@pytest.mark.parametrize("relative_path", ["", ".", "../target"])
def test_invalid_destination_refuses_before_carrier_attempt(plan: IdentityPlan, relative_path: str) -> None:
    carrier = TranscriptCarrier(_begin_records)

    with pytest.raises(ValidationError):
        stage_begin(
            carrier,
            trusted_root_path="/trusted/root",
            relative_path=relative_path,
            token=_TOKEN,
            expected_length=1,
            plan=plan,
            deadline=Deadline.after(1),
            runtime_selection=runtime_selection(),
        )

    assert carrier.calls == 0


def test_invalid_utf8_selector_is_not_retained_by_validation(plan: IdentityPlan) -> None:
    carrier = TranscriptCarrier(_begin_records)

    with pytest.raises(ValidationError) as raised:
        stage_begin(
            carrier,
            trusted_root_path="/private-root-\udcff-canary",
            relative_path="target",
            token=_TOKEN,
            expected_length=1,
            plan=plan,
            deadline=Deadline.after(1),
            runtime_selection=runtime_selection(),
        )

    assert carrier.calls == 0
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "canary" not in repr(raised.value)
