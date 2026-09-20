"""One-attempt host exchanges for private stage creation and chunks."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._file_stage_bundle import FIXED_SOURCE
from agentworks.execution._file_stage_protocol import (
    FileStageBeginRequest,
    FileStageChunkRequest,
    FileStageControlError,
    FileStageFailureCode,
    FileStageFailureControl,
    FileStageOperation,
    FileStageRequest,
    FileStageRequestError,
    encode_file_stage_request,
    parse_empty_file_stage_body,
    parse_file_stage_begin_result,
    parse_file_stage_chunk_result,
    parse_file_stage_failure,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileRecordReader, FileWireError
from agentworks.execution._helper_launcher import IdentityPlan, build_helper_argv
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
    from agentworks.execution._scratch import ScratchReference
    from agentworks.execution.carrier import Carrier, Deadline, ExitStatus

_DEFAULT_RUNTIME = "/usr/bin/python3"


class FileStageObservationState(StrEnum):
    CREATED = "created"
    ACCEPTED = "accepted"
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
    error: FileStageObservationError | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class FileStageCandidateResult:
    """Separate carrier facts from one typed stage observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    observation: FileStageObservation


class FileStageCreationUncertain(Exception):
    """Safe cause retained when creation may have been dispatched."""

    def __init__(self) -> None:
        super().__init__("private stage creation may have been dispatched")


class FileStageChunkUncertain(Exception):
    """Safe cause retained when a chunk may have been dispatched."""

    def __init__(self) -> None:
        super().__init__("private stage chunk may have been dispatched")


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
                self._reference = parse_file_stage_begin_result(
                    record.body,
                    self._request.token,
                    self._request.identity,
                )
            else:
                parse_file_stage_chunk_result(record.body)
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
        return self._reference is not None or self._accepted or self._failure is not None

    def _valid_failure(self, failure: FileStageFailureControl) -> bool:
        if failure.code is not FileStageFailureCode.SCRATCH:
            return True
        expected_phase = (
            ScratchPhase.BEGIN if self._request.operation is FileStageOperation.BEGIN else ScratchPhase.WRITE
        )
        return failure.phase is expected_phase and failure.kind is not None

    def _fail(self, error: FileStageObservationError) -> None:
        if self._error is None:
            self._error = error
        self._clear()

    def _clear(self) -> None:
        self._reference = None
        self._accepted = False
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
        assert self._accepted
        self._clear()
        return FileStageObservation(FileStageObservationState.ACCEPTED)


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
    runtime_path: str,
) -> FileStageCandidateResult:
    fixed_argv = build_helper_argv(plan, runtime_path=runtime_path, fixed_source=FIXED_SOURCE, nonce=request.nonce)
    data = _request_data(request)
    collector = _FileStageCollector(request)
    reader = FileRecordReader(request.nonce, collector.accept)
    stderr = _DiagnosticSink()
    io = CarrierIO(
        input=FiniteInput(data, sensitive=True),
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
        return FileStageCandidateResult(
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
            uncertainty: Exception = (
                FileStageCreationUncertain()
                if request.operation is FileStageOperation.BEGIN
                else FileStageChunkUncertain()
            )
            raise control from uncertainty
        raise


def stage_begin(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    token: bytes,
    expected_length: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_path: str = _DEFAULT_RUNTIME,
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
    return _exchange(carrier, request, plan, deadline, runtime_path)


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
    runtime_path: str = _DEFAULT_RUNTIME,
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
    return _exchange(carrier, request, plan, deadline, runtime_path)
