"""One-attempt host exchanges for private stage transfer and recovery."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._file_stage_bundle import FIXED_BUNDLE
from agentworks.execution._file_stage_protocol import (
    FileStageBeginRequest,
    FileStageChunkRequest,
    FileStageCleanupRequest,
    FileStageControlError,
    FileStageFailureCode,
    FileStageFailureControl,
    FileStageOperation,
    FileStageReconcileRequest,
    FileStageRequest,
    FileStageRequestError,
    _historical_cleanup_shape,
    encode_file_stage_request,
    parse_empty_file_stage_body,
    parse_file_stage_begin_result,
    parse_file_stage_chunk_result,
    parse_file_stage_cleanup_result,
    parse_file_stage_failure,
    parse_file_stage_reconcile_result,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileRecordReader, FileWireError
from agentworks.execution._runtime_prerequisite import (
    RuntimePrefixSink,
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    build_runtime_identity_helper_argv,
)
from agentworks.execution._scratch import ScratchPhase, _cleanup_debt
from agentworks.execution.carrier import (
    CarrierIO,
    Dispatch,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Retention,
    SinkOutput,
)

if TYPE_CHECKING:
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._scratch import ScratchReference
    from agentworks.execution._scratch_receipt import ScratchCleanupDebt
    from agentworks.execution.carrier import Carrier, Deadline, ExitStatus


class FileStageObservationState(StrEnum):
    CREATED = "created"
    ACCEPTED = "accepted"
    RECOVERED = "recovered"
    OWNERSHIP_UNCERTAIN = "ownership_uncertain"
    CLEANED = "cleaned"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"
    UNCERTAIN = "uncertain"


class FileStageObservationError(StrEnum):
    CONTROL = "control"
    ORDER = "order"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    MISSING_TERMINAL = "missing_terminal"


@dataclass(frozen=True, slots=True)
class FileStageObservation:
    state: FileStageObservationState
    reference: ScratchReference | None = field(default=None, repr=False)
    failure: FileStageFailureControl | None = field(default=None, repr=False)
    cleanup_debt: ScratchCleanupDebt | None = field(default=None, repr=False)
    error: FileStageObservationError | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class FileStageCandidateResult:
    """Separate carrier facts from one typed stage observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: FileStageObservation | None


class FileStageCreationUncertain(Exception):
    """Safe cause retained when creation may have been dispatched."""

    def __init__(self) -> None:
        super().__init__("private stage creation may have been dispatched")


class FileStageChunkUncertain(Exception):
    """Safe cause retained when a chunk may have been dispatched."""

    def __init__(self) -> None:
        super().__init__("private stage chunk may have been dispatched")


class FileStageReconcileUncertain(Exception):
    """Safe cause retained when reconciliation observation is unavailable."""

    def __init__(self) -> None:
        super().__init__("private stage reconciliation observation is unavailable")


class FileStageCleanupUncertain(Exception):
    """Safe cause retained when cleanup may have been dispatched."""

    def __init__(self) -> None:
        super().__init__("private stage cleanup may have been dispatched")


def _control_uncertainty(operation: FileStageOperation) -> Exception:
    if operation is FileStageOperation.BEGIN:
        return FileStageCreationUncertain()
    if operation is FileStageOperation.CHUNK:
        return FileStageChunkUncertain()
    if operation is FileStageOperation.RECONCILE:
        return FileStageReconcileUncertain()
    return FileStageCleanupUncertain()


class _DiagnosticSink:
    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        if data:
            self.saw_data = True
        return len(data)


class _FileStageCollector:
    """Reduce one operation-specific transcript without exposing partial facts."""

    def __init__(self, request: FileStageRequest) -> None:
        self._request = request
        self._reference: ScratchReference | None = None
        self._accepted = False
        self._cleanup_debt: ScratchCleanupDebt | None = None
        self._ownership_uncertain = False
        self._failure: FileStageFailureControl | None = None
        self._terminal = False
        self._error: FileStageObservationError | None = None

    def accept(self, record: FileRecord) -> None:
        if self._error is not None:
            return
        if self._terminal:
            self._fail(FileStageObservationError.POST_TERMINAL)
            return
        try:
            self._accept(record)
        except FileStageControlError:
            self._fail(FileStageObservationError.CONTROL)

    def _accept(self, record: FileRecord) -> None:
        if record.kind is FileRecordKind.RESULT:
            if self._has_outcome():
                self._fail(FileStageObservationError.ORDER)
            elif self._request.operation is FileStageOperation.BEGIN:
                reference = parse_file_stage_begin_result(
                    record.body,
                    self._request.token,
                    self._request.identity,
                )
                assert isinstance(self._request, FileStageBeginRequest)
                if reference._ownership._length != self._request.expected_length:
                    self._fail(FileStageObservationError.CONTROL)
                else:
                    self._reference = reference
            elif self._request.operation is FileStageOperation.CHUNK:
                parse_file_stage_chunk_result(record.body)
                self._accepted = True
            elif self._request.operation is FileStageOperation.RECONCILE:
                cleanup = parse_file_stage_reconcile_result(
                    record.body,
                    self._request.token,
                    self._request.identity,
                )
                if cleanup is None:
                    self._ownership_uncertain = True
                else:
                    self._cleanup_debt = cleanup
            else:
                parse_file_stage_cleanup_result(record.body)
                self._accepted = True
            return
        if record.kind is FileRecordKind.FAILED:
            if self._has_outcome():
                self._fail(FileStageObservationError.ORDER)
                return
            failure = parse_file_stage_failure(record.body, self._request.token, self._request.identity)
            if not self._valid_failure(failure):
                self._fail(FileStageObservationError.CONTROL)
            else:
                self._failure = failure
            return
        if record.kind is FileRecordKind.FINISHED:
            parse_empty_file_stage_body(record.body)
            if not self._has_outcome():
                self._fail(FileStageObservationError.ORDER)
            else:
                self._terminal = True
            return
        self._fail(FileStageObservationError.ORDER)

    def _has_outcome(self) -> bool:
        return (
            self._reference is not None
            or self._accepted
            or self._cleanup_debt is not None
            or self._ownership_uncertain
            or self._failure is not None
        )

    def _valid_failure(self, failure: FileStageFailureControl) -> bool:
        if failure.code is not FileStageFailureCode.SCRATCH:
            return True
        expected_phase = {
            FileStageOperation.BEGIN: ScratchPhase.BEGIN,
            FileStageOperation.CHUNK: ScratchPhase.WRITE,
            FileStageOperation.RECONCILE: ScratchPhase.RECONCILE,
            FileStageOperation.CLEANUP: ScratchPhase.CLEANUP,
        }[self._request.operation]
        if failure.phase is not expected_phase:
            return False
        if isinstance(self._request, FileStageChunkRequest | FileStageCleanupRequest):
            expected_debt = (
                _cleanup_debt(self._request.reference)
                if isinstance(self._request, FileStageChunkRequest)
                else self._request.cleanup_debt
            )
            return failure.cleanup_debt == expected_debt
        if isinstance(self._request, FileStageReconcileRequest):
            return failure.cleanup_debt is None or _historical_cleanup_shape(failure.cleanup_debt)
        return True

    def _fail(self, error: FileStageObservationError) -> None:
        if self._error is None:
            self._error = error
        self._clear()

    def _clear(self) -> None:
        self._reference = None
        self._accepted = False
        self._cleanup_debt = None
        self._ownership_uncertain = False
        self._failure = None
        self._terminal = False

    def abort(self) -> None:
        self._clear()

    def finish(
        self,
        wire_error: FileWireError | None,
        *,
        streams_complete: bool,
        stderr_noise: bool,
        dispatch: Dispatch,
    ) -> FileStageObservation:
        error: FileStageObservationError | FileWireError | None = self._error or wire_error
        state = FileStageObservationState.INVALID
        if error is None and stderr_noise:
            error = FileStageObservationError.STDERR
        if error is None and not streams_complete:
            error = FileStageObservationError.CARRIER
            state = FileStageObservationState.INCOMPLETE
        elif error is None and not self._terminal:
            error = FileStageObservationError.MISSING_TERMINAL
            state = FileStageObservationState.INCOMPLETE
        elif error is FileWireError.TRUNCATED:
            state = FileStageObservationState.INCOMPLETE
        if error is not None:
            self._clear()
            if dispatch is not Dispatch.NOT_SENT:
                state = FileStageObservationState.UNCERTAIN
            return FileStageObservation(state, error=error)
        if self._failure is not None:
            failure = self._failure
            self._clear()
            return FileStageObservation(FileStageObservationState.REFUSED, failure=failure)
        if self._reference is not None:
            reference = self._reference
            self._clear()
            return FileStageObservation(FileStageObservationState.CREATED, reference=reference)
        if self._cleanup_debt is not None:
            cleanup_debt = self._cleanup_debt
            self._clear()
            return FileStageObservation(FileStageObservationState.RECOVERED, cleanup_debt=cleanup_debt)
        if self._ownership_uncertain:
            self._clear()
            return FileStageObservation(FileStageObservationState.OWNERSHIP_UNCERTAIN)
        assert self._accepted
        self._clear()
        state = (
            FileStageObservationState.CLEANED
            if self._request.operation is FileStageOperation.CLEANUP
            else FileStageObservationState.ACCEPTED
        )
        return FileStageObservation(state)


def _validate_text(value: object) -> str:
    if type(value) is not str or "\0" in value:
        raise ValidationError("File-stage paths must be non-NUL UTF-8 strings")
    failed = False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        failed = True
    if failed:
        raise ValidationError("File-stage paths must be non-NUL UTF-8 strings")
    return value


def _request_data(request: FileStageRequest) -> bytes:
    try:
        return encode_file_stage_request(request)
    except FileStageRequestError as error:
        failure = error.failure
    if failure is FileStageFailureCode.OVERSIZED_REQUEST:
        raise ValidationError("File-stage request exceeds the 32768-byte manifest bound") from None
    raise ValidationError("File-stage request contains an invalid field") from None


def _exchange(
    carrier: Carrier,
    request: FileStageRequest,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FileStageCandidateResult:
    fixed_argv, candidates, system_shim = build_runtime_identity_helper_argv(
        plan,
        selection=runtime_selection,
        fixed_source=FIXED_BUNDLE.bootstrap,
        nonce=request.nonce,
    )
    data = _request_data(request)
    collector = _FileStageCollector(request)
    reader = FileRecordReader(request.nonce, collector.accept)
    runtime = RuntimePrefixSink(request.nonce, candidates, reader, system_shim)
    stderr = _DiagnosticSink()
    io = CarrierIO(
        input=FiniteInput(FIXED_BUNDLE.prefix + data, sensitive=True),
        output=SinkOutput(runtime, stderr, require_live=False),
        sensitive=True,
    )
    dispatch = Dispatch.UNKNOWN
    try:
        report = carrier.execute(PreparedInvocation(fixed_argv), io=io, deadline=deadline)
        dispatch = report.dispatch
        runtime_prerequisite = runtime.observation
        if runtime_prerequisite.state is RuntimePrerequisiteState.READY:
            reader.finish()
            delivered = (
                report.stdout.retention is Retention.DELIVERED and report.stderr.retention is Retention.DELIVERED
            )
            observation = collector.finish(
                reader.error,
                streams_complete=delivered and report.stdout.complete and report.stderr.complete,
                stderr_noise=stderr.saw_data,
                dispatch=report.dispatch,
            )
        else:
            reader.abort()
            collector.abort()
            observation = None
        return FileStageCandidateResult(
            report.dispatch,
            report.completion,
            report.local_status,
            report.failure,
            runtime_prerequisite,
            observation,
        )
    except BaseException as control:
        reader.abort()
        collector.abort()
        if dispatch is not Dispatch.NOT_SENT:
            raise control from _control_uncertainty(request.operation)
        raise
    finally:
        runtime.clear()


def stage_begin(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    token: bytes,
    expected_length: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FileStageCandidateResult:
    """Create one private stage through one fresh non-replayed attempt."""
    request = FileStageBeginRequest(
        secrets.token_hex(16),
        _validate_text(trusted_root_path),
        _validate_text(relative_path),
        token,
        expected_length,
        plan.expected,
        deadline.remaining(),
    )
    return _exchange(carrier, request, plan, deadline, runtime_selection)


def stage_chunk(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    token: bytes,
    reference: ScratchReference,
    offset: int,
    data: bytes,
    chunk_digest: bytes,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FileStageCandidateResult:
    """Write one bounded stage chunk through one fresh non-replayed attempt."""
    request = FileStageChunkRequest(
        secrets.token_hex(16),
        _validate_text(trusted_root_path),
        _validate_text(relative_path),
        token,
        reference,
        offset,
        data,
        chunk_digest,
        plan.expected,
        deadline.remaining(),
    )
    return _exchange(carrier, request, plan, deadline, runtime_selection)


def stage_reconcile(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    token: bytes,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FileStageCandidateResult:
    """Read one exact stage receipt without replaying creation or promoting it."""
    request = FileStageReconcileRequest(
        secrets.token_hex(16),
        _validate_text(trusted_root_path),
        _validate_text(relative_path),
        token,
        plan.expected,
        deadline.remaining(),
    )
    return _exchange(carrier, request, plan, deadline, runtime_selection)


def stage_cleanup(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    token: bytes,
    cleanup_debt: ScratchCleanupDebt,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FileStageCandidateResult:
    """Attempt exact cleanup once without inferring terminal quiescence."""
    request = FileStageCleanupRequest(
        secrets.token_hex(16),
        _validate_text(trusted_root_path),
        _validate_text(relative_path),
        token,
        cleanup_debt,
        plan.expected,
        deadline.remaining(),
    )
    return _exchange(carrier, request, plan, deadline, runtime_selection)
