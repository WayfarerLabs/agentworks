"""One-attempt host exchanges for private scratch-backed publication."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._file_publication import (
    Create,
    CreateMetadata,
    Match,
    PublicationFailureKind,
    PublicationPhase,
    Replace,
)
from agentworks.execution._file_publication_bundle import FIXED_BUNDLE
from agentworks.execution._file_publication_protocol import (
    FilePublicationCleanupRequest,
    FilePublicationControlError,
    FilePublicationFailureCode,
    FilePublicationFailureControl,
    FilePublicationOperation,
    FilePublicationReconcileRequest,
    FilePublicationRequest,
    FilePublicationRequestError,
    FilePublishRequest,
    PublicationCleanupState,
    encode_file_publication_request,
    parse_empty_file_publication_body,
    parse_file_publication_cleanup_result,
    parse_file_publication_failure,
    parse_file_publication_reconcile_result,
    parse_file_publish_result,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileRecordReader, FileWireError
from agentworks.execution._publication_receipt import PublicationStageCleanupDebt
from agentworks.execution._runtime_prerequisite import (
    RuntimePrefixSink,
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    build_runtime_identity_helper_argv,
)
from agentworks.execution._scratch import ScratchPhase
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
    from agentworks.execution._file_publication_wire import BoundPublicationCleanupDebt
    from agentworks.execution._file_stat import FileRevision
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._scratch import ScratchReference
    from agentworks.execution.carrier import Carrier, Deadline, ExitStatus


class FilePublicationObservationState(StrEnum):
    PUBLISHED = "published"
    RECOVERED = "recovered"
    OWNERSHIP_UNCERTAIN = "ownership_uncertain"
    CLEANED = "cleaned"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"
    UNCERTAIN = "uncertain"


class FilePublicationObservationError(StrEnum):
    CONTROL = "control"
    ORDER = "order"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    MISSING_TERMINAL = "missing_terminal"


@dataclass(frozen=True, slots=True)
class FilePublicationObservation:
    state: FilePublicationObservationState
    revision: FileRevision | None = field(default=None, repr=False)
    cleanup_debt: BoundPublicationCleanupDebt | None = field(default=None, repr=False)
    failure: FilePublicationFailureControl | None = field(default=None, repr=False)
    deadline_exceeded: bool | None = None
    error: FilePublicationObservationError | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class FilePublicationCandidateResult:
    """Separate carrier facts from one typed publication observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: FilePublicationObservation | None


class FilePublishUncertain(Exception):
    def __init__(self) -> None:
        super().__init__("private publication may have been dispatched")


class FilePublicationReconcileUncertain(Exception):
    def __init__(self) -> None:
        super().__init__("private publication reconciliation observation is unavailable")


class FilePublicationCleanupUncertain(Exception):
    def __init__(self) -> None:
        super().__init__("private publication cleanup may have been dispatched")


def _control_uncertainty(operation: FilePublicationOperation) -> Exception:
    if operation is FilePublicationOperation.PUBLISH:
        return FilePublishUncertain()
    if operation is FilePublicationOperation.RECONCILE:
        return FilePublicationReconcileUncertain()
    return FilePublicationCleanupUncertain()


class _DiagnosticSink:
    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        if data:
            self.saw_data = True
        return len(data)


def _cleanup_progress(
    original: BoundPublicationCleanupDebt,
    observed: BoundPublicationCleanupDebt | None,
) -> bool:
    if observed is None:
        return False
    if observed._reference != original._reference or observed._publication_parent != original._publication_parent:
        return False
    expected = original._debt
    candidate = observed._debt
    if isinstance(expected, PublicationStageCleanupDebt):
        return (
            isinstance(candidate, PublicationStageCleanupDebt)
            and candidate._ownership == expected._ownership
            and (candidate._stage_removed or not expected._stage_removed)
        )
    return candidate == expected


def _cleanup_failure_matches_input(
    request: FilePublicationCleanupRequest,
    failure: FilePublicationFailureControl,
) -> bool:
    if failure.cleanup_state is PublicationCleanupState.OWNERSHIP_UNCERTAIN:
        return failure.cleanup_debt is None
    return failure.cleanup_state is PublicationCleanupState.EXACT and _cleanup_progress(
        request.cleanup_debt, failure.cleanup_debt
    )


class _FilePublicationCollector:
    """Reduce one closed transcript without retaining partial publication facts."""

    def __init__(self, request: FilePublicationRequest) -> None:
        self._request = request
        self._outcome: FilePublicationObservation | None = None
        self._terminal = False
        self._error: FilePublicationObservationError | None = None

    def accept(self, record: FileRecord) -> None:
        if self._error is not None:
            return
        if self._terminal:
            self._fail(FilePublicationObservationError.POST_TERMINAL)
            return
        try:
            self._accept(record)
        except FilePublicationControlError:
            self._fail(FilePublicationObservationError.CONTROL)

    def _accept(self, record: FileRecord) -> None:
        if record.kind is FileRecordKind.RESULT:
            if self._outcome is not None:
                self._fail(FilePublicationObservationError.ORDER)
            elif isinstance(self._request, FilePublishRequest):
                publish_result = parse_file_publish_result(
                    record.body,
                    self._request.digest,
                    self._request.reference._ownership._length,
                )
                self._outcome = FilePublicationObservation(
                    FilePublicationObservationState.PUBLISHED,
                    revision=publish_result.revision,
                    deadline_exceeded=publish_result.deadline_exceeded,
                )
            elif isinstance(self._request, FilePublicationReconcileRequest):
                reconcile_result = parse_file_publication_reconcile_result(record.body, self._request.reference)
                if reconcile_result.cleanup_debt is None:
                    self._outcome = FilePublicationObservation(
                        FilePublicationObservationState.OWNERSHIP_UNCERTAIN,
                        deadline_exceeded=reconcile_result.deadline_exceeded,
                    )
                else:
                    self._outcome = FilePublicationObservation(
                        FilePublicationObservationState.RECOVERED,
                        cleanup_debt=reconcile_result.cleanup_debt,
                        deadline_exceeded=reconcile_result.deadline_exceeded,
                    )
            else:
                cleanup_result = parse_file_publication_cleanup_result(record.body)
                self._outcome = FilePublicationObservation(
                    FilePublicationObservationState.CLEANED,
                    deadline_exceeded=cleanup_result.deadline_exceeded,
                )
            return
        if record.kind is FileRecordKind.FAILED:
            if self._outcome is not None:
                self._fail(FilePublicationObservationError.ORDER)
                return
            failure = parse_file_publication_failure(record.body, self._request.reference)
            if not self._valid_failure(failure):
                self._fail(FilePublicationObservationError.CONTROL)
            else:
                state = (
                    FilePublicationObservationState.UNCERTAIN
                    if failure.code is FilePublicationFailureCode.PUBLICATION
                    and failure.publication_kind is PublicationFailureKind.UNCERTAIN
                    else FilePublicationObservationState.REFUSED
                )
                self._outcome = FilePublicationObservation(
                    state,
                    cleanup_debt=failure.cleanup_debt,
                    failure=failure,
                )
            return
        if record.kind is FileRecordKind.FINISHED:
            parse_empty_file_publication_body(record.body)
            if self._outcome is None:
                self._fail(FilePublicationObservationError.ORDER)
            else:
                self._terminal = True
            return
        self._fail(FilePublicationObservationError.ORDER)

    def _valid_failure(self, failure: FilePublicationFailureControl) -> bool:
        if failure.code is FilePublicationFailureCode.SCRATCH:
            return isinstance(self._request, FilePublishRequest) and failure.scratch_phase is ScratchPhase.VERIFY
        if failure.code is FilePublicationFailureCode.PUBLICATION:
            if isinstance(self._request, FilePublishRequest):
                return True
            if not isinstance(self._request, FilePublicationCleanupRequest):
                return False
            return failure.publication_phase is PublicationPhase.CLEANUP and _cleanup_failure_matches_input(
                self._request, failure
            )
        if failure.code is FilePublicationFailureCode.RECEIPT:
            if isinstance(self._request, FilePublicationReconcileRequest):
                return failure.cleanup_state is PublicationCleanupState.NONE
            if not isinstance(self._request, FilePublicationCleanupRequest):
                return False
            return isinstance(
                self._request.cleanup_debt._debt, PublicationStageCleanupDebt
            ) and _cleanup_failure_matches_input(self._request, failure)
        return True

    def _fail(self, error: FilePublicationObservationError) -> None:
        if self._error is None:
            self._error = error
        self._clear()

    def _clear(self) -> None:
        self._outcome = None
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
    ) -> FilePublicationObservation:
        error: FilePublicationObservationError | FileWireError | None = self._error or wire_error
        state = FilePublicationObservationState.INVALID
        if error is None and stderr_noise:
            error = FilePublicationObservationError.STDERR
        if error is None and not streams_complete:
            error = FilePublicationObservationError.CARRIER
            state = FilePublicationObservationState.INCOMPLETE
        elif error is None and not self._terminal:
            error = FilePublicationObservationError.MISSING_TERMINAL
            state = FilePublicationObservationState.INCOMPLETE
        elif error is FileWireError.TRUNCATED:
            state = FilePublicationObservationState.INCOMPLETE
        if error is not None:
            self._clear()
            if dispatch is not Dispatch.NOT_SENT:
                state = FilePublicationObservationState.UNCERTAIN
            return FilePublicationObservation(state, error=error)
        outcome = self._outcome
        assert outcome is not None
        self._clear()
        return outcome


def _validate_text(value: object) -> str:
    if type(value) is not str or "\0" in value:
        raise ValidationError("File-publication paths must be non-NUL UTF-8 strings")
    failed = False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        failed = True
    if failed:
        raise ValidationError("File-publication paths must be non-NUL UTF-8 strings")
    return value


def _request_data(request: FilePublicationRequest) -> bytes:
    try:
        return encode_file_publication_request(request)
    except FilePublicationRequestError as error:
        failure = error.failure
    if failure is FilePublicationFailureCode.OVERSIZED_REQUEST:
        raise ValidationError("File-publication request exceeds the 32768-byte manifest bound") from None
    raise ValidationError("File-publication request contains an invalid field") from None


def _exchange(
    carrier: Carrier,
    request: FilePublicationRequest,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FilePublicationCandidateResult:
    data = _request_data(request)
    fixed_argv, candidates, system_shim = build_runtime_identity_helper_argv(
        plan,
        selection=runtime_selection,
        fixed_source=FIXED_BUNDLE.bootstrap,
        nonce=request.nonce,
    )
    collector = _FilePublicationCollector(request)
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
        return FilePublicationCandidateResult(
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


def publish(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    token: bytes,
    reference: ScratchReference,
    digest: bytes,
    condition: Create | Replace | Match,
    create_metadata: CreateMetadata,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FilePublicationCandidateResult:
    """Publish one verified stage through one fresh non-replayed attempt."""
    request = FilePublishRequest(
        secrets.token_hex(16),
        token,
        _validate_text(trusted_root_path),
        _validate_text(relative_path),
        reference,
        digest,
        condition,
        create_metadata,
        plan.expected,
        deadline.remaining(),
    )
    return _exchange(carrier, request, plan, deadline, runtime_selection)


def publication_reconcile(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    token: bytes,
    reference: ScratchReference,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FilePublicationCandidateResult:
    """Recover historical cleanup ownership without inferring publication."""
    request = FilePublicationReconcileRequest(
        secrets.token_hex(16),
        token,
        _validate_text(trusted_root_path),
        _validate_text(relative_path),
        reference,
        plan.expected,
        deadline.remaining(),
    )
    return _exchange(carrier, request, plan, deadline, runtime_selection)


def publication_cleanup(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    token: bytes,
    reference: ScratchReference,
    cleanup_debt: BoundPublicationCleanupDebt,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FilePublicationCandidateResult:
    """Attempt one exact publication cleanup without a quiescence claim."""
    request = FilePublicationCleanupRequest(
        secrets.token_hex(16),
        token,
        _validate_text(trusted_root_path),
        _validate_text(relative_path),
        reference,
        cleanup_debt,
        plan.expected,
        deadline.remaining(),
    )
    return _exchange(carrier, request, plan, deadline, runtime_selection)
