"""One-attempt host exchanges for private snapshot transfer and recovery."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._file_snapshot_bundle import FIXED_SOURCE
from agentworks.execution._file_snapshot_protocol import (
    MAX_SNAPSHOT_CHUNK_BYTES,
    FileSnapshotBeginRequest,
    FileSnapshotChunkRequest,
    FileSnapshotChunkResult,
    FileSnapshotCleanupRequest,
    FileSnapshotControlError,
    FileSnapshotFailureCode,
    FileSnapshotFailureControl,
    FileSnapshotOperation,
    FileSnapshotReconcileRequest,
    FileSnapshotRequest,
    FileSnapshotRequestError,
    _historical_cleanup_shape,
    encode_file_snapshot_request,
    parse_empty_file_snapshot_body,
    parse_file_snapshot_begin_result,
    parse_file_snapshot_chunk_result,
    parse_file_snapshot_cleanup_result,
    parse_file_snapshot_failure,
    parse_file_snapshot_reconcile_result,
)
from agentworks.execution._file_wire import (
    MAX_RECORD_BODY_BYTES,
    FileRecord,
    FileRecordKind,
    FileRecordReader,
    FileWireError,
)
from agentworks.execution._helper_launcher import IdentityPlan, build_helper_argv
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
    from agentworks.execution._file_spool import SpoolSnapshot
    from agentworks.execution._scratch import ReadyScratchReference
    from agentworks.execution._scratch_receipt import ScratchCleanupDebt
    from agentworks.execution.carrier import Carrier, Deadline, ExitStatus

_DEFAULT_RUNTIME = "/usr/bin/python3"
_MAX_DATA_RECORDS = (MAX_SNAPSHOT_CHUNK_BYTES + MAX_RECORD_BODY_BYTES - 1) // MAX_RECORD_BODY_BYTES


class FileSnapshotObservationState(StrEnum):
    READY = "ready"
    CHUNK = "chunk"
    ABSENT = "absent"
    RECOVERED = "recovered"
    OWNERSHIP_UNCERTAIN = "ownership_uncertain"
    CLEANED = "cleaned"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"
    UNCERTAIN = "uncertain"


class FileSnapshotObservationError(StrEnum):
    CONTROL = "control"
    ORDER = "order"
    CONTENT = "content"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    MISSING_TERMINAL = "missing_terminal"


@dataclass(frozen=True, slots=True)
class FileSnapshotObservation:
    state: FileSnapshotObservationState
    snapshot: SpoolSnapshot | None = field(default=None, repr=False)
    chunk: FileSnapshotChunkResult | None = field(default=None, repr=False)
    failure: FileSnapshotFailureControl | None = field(default=None, repr=False)
    cleanup_debt: ScratchCleanupDebt | None = field(default=None, repr=False)
    error: FileSnapshotObservationError | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class FileSnapshotCandidateResult:
    """Separate carrier facts from one typed snapshot observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    observation: FileSnapshotObservation


class FileSnapshotCreationUncertain(Exception):
    def __init__(self) -> None:
        super().__init__("private snapshot creation may have been dispatched")


class FileSnapshotChunkUncertain(Exception):
    def __init__(self) -> None:
        super().__init__("private snapshot chunk observation is unavailable")


class FileSnapshotReconcileUncertain(Exception):
    def __init__(self) -> None:
        super().__init__("private snapshot reconciliation observation is unavailable")


class FileSnapshotCleanupUncertain(Exception):
    def __init__(self) -> None:
        super().__init__("private snapshot cleanup may have been dispatched")


def _control_uncertainty(operation: FileSnapshotOperation) -> Exception:
    if operation is FileSnapshotOperation.BEGIN:
        return FileSnapshotCreationUncertain()
    if operation is FileSnapshotOperation.CHUNK:
        return FileSnapshotChunkUncertain()
    if operation is FileSnapshotOperation.RECONCILE:
        return FileSnapshotReconcileUncertain()
    return FileSnapshotCleanupUncertain()


class _DiagnosticSink:
    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        if data:
            self.saw_data = True
        return len(data)


class _FileSnapshotCollector:
    """Reduce one closed transcript while retaining only verified typed facts."""

    def __init__(self, request: FileSnapshotRequest) -> None:
        self._request = request
        self._data = bytearray()
        self._data_records = 0
        self._short_data_record = False
        self._snapshot: SpoolSnapshot | None = None
        self._chunk: FileSnapshotChunkResult | None = None
        self._absent = False
        self._cleanup_debt: ScratchCleanupDebt | None = None
        self._ownership_uncertain = False
        self._cleaned = False
        self._failure: FileSnapshotFailureControl | None = None
        self._terminal = False
        self._error: FileSnapshotObservationError | None = None

    def accept(self, record: FileRecord) -> None:
        if self._error is not None:
            return
        if self._terminal:
            self._fail(FileSnapshotObservationError.POST_TERMINAL)
            return
        try:
            self._accept(record)
        except FileSnapshotControlError:
            self._fail(FileSnapshotObservationError.CONTROL)

    def _accept(self, record: FileRecord) -> None:
        if record.kind is FileRecordKind.DATA:
            if not isinstance(self._request, FileSnapshotChunkRequest) or self._has_outcome():
                self._fail(FileSnapshotObservationError.ORDER)
            elif (
                not record.body
                or self._short_data_record
                or self._data_records >= _MAX_DATA_RECORDS
                or len(self._data) + len(record.body) > self._request.length
            ):
                self._fail(FileSnapshotObservationError.CONTENT)
            else:
                self._data.extend(record.body)
                self._data_records += 1
                self._short_data_record = len(record.body) < MAX_RECORD_BODY_BYTES
            return
        if record.kind is FileRecordKind.RESULT:
            if self._has_outcome():
                self._fail(FileSnapshotObservationError.ORDER)
            elif isinstance(self._request, FileSnapshotBeginRequest):
                result = parse_file_snapshot_begin_result(
                    record.body,
                    self._request.token,
                    self._request.identity,
                    self._request.max_bytes,
                )
                if result is None:
                    self._absent = True
                else:
                    self._snapshot = result
            elif isinstance(self._request, FileSnapshotChunkRequest):
                self._chunk = parse_file_snapshot_chunk_result(
                    record.body,
                    self._request.offset,
                    self._request.length,
                    bytes(self._data),
                )
            elif isinstance(self._request, FileSnapshotReconcileRequest):
                cleanup = parse_file_snapshot_reconcile_result(
                    record.body,
                    self._request.token,
                    self._request.identity,
                )
                if cleanup is None:
                    self._ownership_uncertain = True
                else:
                    self._cleanup_debt = cleanup
            else:
                parse_file_snapshot_cleanup_result(record.body)
                self._cleaned = True
            return
        if record.kind is FileRecordKind.FAILED:
            if self._data or self._has_outcome():
                self._fail(FileSnapshotObservationError.ORDER)
                return
            failure = parse_file_snapshot_failure(record.body, self._request.token, self._request.identity)
            if self._valid_failure(failure):
                self._failure = failure
            else:
                self._fail(FileSnapshotObservationError.CONTROL)
            return
        if record.kind is FileRecordKind.FINISHED:
            parse_empty_file_snapshot_body(record.body)
            if not self._has_outcome():
                self._fail(FileSnapshotObservationError.ORDER)
            else:
                self._terminal = True
            return
        self._fail(FileSnapshotObservationError.ORDER)

    def _has_outcome(self) -> bool:
        return (
            self._snapshot is not None
            or self._chunk is not None
            or self._absent
            or self._cleanup_debt is not None
            or self._ownership_uncertain
            or self._cleaned
            or self._failure is not None
        )

    def _valid_failure(self, failure: FileSnapshotFailureControl) -> bool:
        if failure.code is FileSnapshotFailureCode.ROOT_REFUSED:
            return isinstance(self._request, FileSnapshotBeginRequest)
        if failure.code is FileSnapshotFailureCode.SPOOL:
            return isinstance(self._request, FileSnapshotBeginRequest)
        if failure.code is not FileSnapshotFailureCode.SCRATCH:
            return True
        expected_phase = {
            FileSnapshotChunkRequest: ScratchPhase.READ,
            FileSnapshotReconcileRequest: ScratchPhase.RECONCILE,
            FileSnapshotCleanupRequest: ScratchPhase.CLEANUP,
        }.get(type(self._request))
        if failure.scratch_phase is not expected_phase or expected_phase is None:
            return False
        if isinstance(self._request, FileSnapshotChunkRequest):
            return failure.cleanup_debt == _cleanup_debt(self._request.ready)
        if isinstance(self._request, FileSnapshotCleanupRequest):
            return failure.cleanup_debt == self._request.cleanup_debt
        return failure.cleanup_debt is None or _historical_cleanup_shape(failure.cleanup_debt)

    def _fail(self, error: FileSnapshotObservationError) -> None:
        if self._error is None:
            self._error = error
        self._clear()

    def _clear(self) -> None:
        self._data.clear()
        self._data_records = 0
        self._short_data_record = False
        self._snapshot = None
        self._chunk = None
        self._absent = False
        self._cleanup_debt = None
        self._ownership_uncertain = False
        self._cleaned = False
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
    ) -> FileSnapshotObservation:
        error: FileSnapshotObservationError | FileWireError | None = self._error or wire_error
        state = FileSnapshotObservationState.INVALID
        if error is None and stderr_noise:
            error = FileSnapshotObservationError.STDERR
        if error is None and not streams_complete:
            error = FileSnapshotObservationError.CARRIER
            state = FileSnapshotObservationState.INCOMPLETE
        elif error is None and not self._terminal:
            error = FileSnapshotObservationError.MISSING_TERMINAL
            state = FileSnapshotObservationState.INCOMPLETE
        elif error is FileWireError.TRUNCATED:
            state = FileSnapshotObservationState.INCOMPLETE
        if error is not None:
            self._clear()
            if dispatch is not Dispatch.NOT_SENT:
                state = FileSnapshotObservationState.UNCERTAIN
            return FileSnapshotObservation(state, error=error)
        if self._failure is not None:
            failure = self._failure
            self._clear()
            return FileSnapshotObservation(FileSnapshotObservationState.REFUSED, failure=failure)
        if self._snapshot is not None:
            snapshot = self._snapshot
            self._clear()
            return FileSnapshotObservation(FileSnapshotObservationState.READY, snapshot=snapshot)
        if self._chunk is not None:
            chunk = self._chunk
            self._clear()
            return FileSnapshotObservation(FileSnapshotObservationState.CHUNK, chunk=chunk)
        if self._absent:
            self._clear()
            return FileSnapshotObservation(FileSnapshotObservationState.ABSENT)
        if self._cleanup_debt is not None:
            cleanup = self._cleanup_debt
            self._clear()
            return FileSnapshotObservation(FileSnapshotObservationState.RECOVERED, cleanup_debt=cleanup)
        if self._ownership_uncertain:
            self._clear()
            return FileSnapshotObservation(FileSnapshotObservationState.OWNERSHIP_UNCERTAIN)
        assert self._cleaned
        self._clear()
        return FileSnapshotObservation(FileSnapshotObservationState.CLEANED)


def _validate_text(value: object) -> str:
    if type(value) is not str or "\0" in value:
        raise ValidationError("File-snapshot paths must be valid non-NUL UTF-8 strings")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError("File-snapshot paths must be valid non-NUL UTF-8 strings") from None
    return value


def _request_data(request: FileSnapshotRequest) -> bytes:
    try:
        return encode_file_snapshot_request(request)
    except FileSnapshotRequestError as error:
        failure = error.failure
    if failure is FileSnapshotFailureCode.OVERSIZED_REQUEST:
        raise ValidationError("File-snapshot request exceeds the 32768-byte manifest bound") from None
    raise ValidationError("File-snapshot request contains an invalid field") from None


def _exchange(
    carrier: Carrier,
    request: FileSnapshotRequest,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_path: str,
) -> FileSnapshotCandidateResult:
    fixed_argv = build_helper_argv(plan, runtime_path=runtime_path, fixed_source=FIXED_SOURCE, nonce=request.nonce)
    collector = _FileSnapshotCollector(request)
    reader = FileRecordReader(request.nonce, collector.accept)
    stderr = _DiagnosticSink()
    io = CarrierIO(
        input=FiniteInput(_request_data(request), sensitive=True),
        output=SinkOutput(reader, stderr, require_live=False),
        sensitive=True,
    )
    dispatch = Dispatch.UNKNOWN
    try:
        report = carrier.execute(PreparedInvocation(fixed_argv), io=io, deadline=deadline)
        dispatch = report.dispatch
        reader.finish()
        delivered = report.stdout.retention is Retention.DELIVERED and report.stderr.retention is Retention.DELIVERED
        observation = collector.finish(
            reader.error,
            streams_complete=delivered and report.stdout.complete and report.stderr.complete,
            stderr_noise=stderr.saw_data,
            dispatch=report.dispatch,
        )
        return FileSnapshotCandidateResult(
            report.dispatch,
            report.completion,
            report.local_status,
            report.failure,
            observation,
        )
    except BaseException as control:
        reader.abort()
        collector.abort()
        if dispatch is not Dispatch.NOT_SENT:
            raise control from _control_uncertainty(request.operation)
        raise


def snapshot_begin(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    max_bytes: int,
    token: bytes,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_path: str = _DEFAULT_RUNTIME,
) -> FileSnapshotCandidateResult:
    """Create one immutable private snapshot through one fresh attempt."""
    request = FileSnapshotBeginRequest(
        secrets.token_hex(16),
        token,
        _validate_text(trusted_root_path),
        _validate_text(relative_path),
        max_bytes,
        plan.expected,
        deadline.remaining(),
    )
    return _exchange(carrier, request, plan, deadline, runtime_path)


def snapshot_chunk(
    carrier: Carrier,
    *,
    token: bytes,
    ready: ReadyScratchReference,
    offset: int,
    length: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_path: str = _DEFAULT_RUNTIME,
) -> FileSnapshotCandidateResult:
    """Read one exact bounded range from an immutable private snapshot."""
    request = FileSnapshotChunkRequest(
        secrets.token_hex(16),
        token,
        ready,
        offset,
        length,
        plan.expected,
        deadline.remaining(),
    )
    return _exchange(carrier, request, plan, deadline, runtime_path)


def snapshot_reconcile(
    carrier: Carrier,
    *,
    token: bytes,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_path: str = _DEFAULT_RUNTIME,
) -> FileSnapshotCandidateResult:
    """Read historical cleanup ownership without replaying snapshot creation."""
    request = FileSnapshotReconcileRequest(
        secrets.token_hex(16),
        token,
        plan.expected,
        deadline.remaining(),
    )
    return _exchange(carrier, request, plan, deadline, runtime_path)


def snapshot_cleanup(
    carrier: Carrier,
    *,
    token: bytes,
    cleanup_debt: ScratchCleanupDebt,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_path: str = _DEFAULT_RUNTIME,
) -> FileSnapshotCandidateResult:
    """Attempt exact identity-bound cleanup once without a quiescence claim."""
    request = FileSnapshotCleanupRequest(
        secrets.token_hex(16),
        token,
        cleanup_debt,
        plan.expected,
        deadline.remaining(),
    )
    return _exchange(carrier, request, plan, deadline, runtime_path)
