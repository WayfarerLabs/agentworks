"""One-attempt host exchanges for Linux file-object stat and removal."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._file_object_bundle import FIXED_SOURCE
from agentworks.execution._file_object_protocol import (
    FileObjectControlError,
    FileObjectFailureCode,
    FileObjectFailureControl,
    FileObjectOperation,
    FileObjectRequest,
    FileObjectRequestError,
    FileObjectResultControl,
    encode_file_object_request,
    parse_empty_file_object_body,
    parse_file_object_failure,
    parse_file_object_result,
)
from agentworks.execution._file_objects import FileKind, FileObjectFailureKind, FileObjectPhase
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileRecordReader, FileWireError
from agentworks.execution._helper_launcher import IdentityPlan, build_helper_argv
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
    from agentworks.execution._file_stat import FileRevision
    from agentworks.execution.carrier import Carrier, Deadline, ExitStatus

_DEFAULT_RUNTIME = "/usr/bin/python3"


class FileObjectObservationState(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"
    UNCERTAIN = "uncertain"


class FileObjectObservationError(StrEnum):
    CONTROL = "control"
    ORDER = "order"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    MISSING_TERMINAL = "missing_terminal"


@dataclass(frozen=True, slots=True)
class FileObjectObservation:
    state: FileObjectObservationState
    revision: FileRevision | None = field(default=None, repr=False)
    object_kind: FileKind | None = None
    failure: FileObjectFailureControl | None = None
    error: FileObjectObservationError | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class FileObjectCandidateResult:
    """Separate raw carrier facts from one reduced typed helper observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    observation: FileObjectObservation


class FileObjectMutationUncertain(Exception):
    """Safe cause retained when control escapes a possibly dispatched removal."""

    def __init__(self) -> None:
        super().__init__("file-object removal may have been dispatched")


class _DiagnosticSink:
    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        if data:
            self.saw_data = True
        return len(data)


class _FileObjectCollector:
    """Reduce the operation-specific grammar without retaining raw records."""

    def __init__(self, operation: FileObjectOperation) -> None:
        self._operation = operation
        self._result: FileObjectResultControl | None = None
        self._failure: FileObjectFailureControl | None = None
        self._terminal = False
        self._error: FileObjectObservationError | None = None

    def accept(self, record: FileRecord) -> None:
        if self._error is not None:
            return
        if self._terminal:
            self._fail(FileObjectObservationError.POST_TERMINAL)
            return
        try:
            if record.kind is FileRecordKind.RESULT:
                if self._result is not None or self._failure is not None:
                    self._fail(FileObjectObservationError.ORDER)
                else:
                    self._result = parse_file_object_result(record.body, self._operation)
            elif record.kind is FileRecordKind.FAILED:
                if self._result is not None or self._failure is not None:
                    self._fail(FileObjectObservationError.ORDER)
                else:
                    failure = parse_file_object_failure(record.body)
                    if not self._valid_failure(failure):
                        self._fail(FileObjectObservationError.CONTROL)
                    else:
                        self._failure = failure
            elif record.kind is FileRecordKind.FINISHED:
                parse_empty_file_object_body(record.body)
                if (self._result is None) == (self._failure is None):
                    self._fail(FileObjectObservationError.ORDER)
                else:
                    self._terminal = True
            else:
                self._fail(FileObjectObservationError.ORDER)
        except FileObjectControlError:
            self._fail(FileObjectObservationError.CONTROL)

    def _valid_failure(self, failure: FileObjectFailureControl) -> bool:
        if failure.code is not FileObjectFailureCode.OBJECT:
            return True
        if self._operation is FileObjectOperation.STAT:
            return failure.phase is FileObjectPhase.OBSERVATION and failure.kind is not FileObjectFailureKind.UNCERTAIN
        return failure.phase in {FileObjectPhase.CONDITION, FileObjectPhase.REMOVAL}

    def _fail(self, error: FileObjectObservationError) -> None:
        if self._error is None:
            self._error = error
        self._clear()

    def _clear(self) -> None:
        self._result = None
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
    ) -> FileObjectObservation:
        error: FileObjectObservationError | FileWireError | None = self._error or wire_error
        state = FileObjectObservationState.INVALID
        if error is None and stderr_noise:
            error = FileObjectObservationError.STDERR
        if error is None and not streams_complete:
            error = FileObjectObservationError.CARRIER
            state = FileObjectObservationState.INCOMPLETE
        elif error is None and not self._terminal:
            error = FileObjectObservationError.MISSING_TERMINAL
            state = FileObjectObservationState.INCOMPLETE
        elif error is FileWireError.TRUNCATED:
            state = FileObjectObservationState.INCOMPLETE
        if error is not None:
            self._clear()
            if self._operation is FileObjectOperation.REMOVE and dispatch is not Dispatch.NOT_SENT:
                state = FileObjectObservationState.UNCERTAIN
            return FileObjectObservation(state, error=error)
        if self._failure is not None:
            failure = self._failure
            self._clear()
            failure_state = (
                FileObjectObservationState.UNCERTAIN
                if failure.code is FileObjectFailureCode.OBJECT and failure.kind is FileObjectFailureKind.UNCERTAIN
                else FileObjectObservationState.REFUSED
            )
            return FileObjectObservation(failure_state, failure=failure)
        assert self._result is not None
        result = self._result
        self._clear()
        return FileObjectObservation(
            FileObjectObservationState(result.kind.value),
            revision=result.revision,
            object_kind=result.object_kind,
        )


def _validate_text(value: object) -> str:
    if type(value) is not str or "\0" in value:
        raise ValidationError("File-object paths must be valid non-NUL UTF-8 strings")
    failed = False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        failed = True
    if failed:
        raise ValidationError("File-object paths must be valid non-NUL UTF-8 strings")
    return value


def _exchange(
    carrier: Carrier,
    *,
    operation: FileObjectOperation,
    trusted_root_path: str,
    relative_path: str,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_path: str,
    expected_kind: FileKind | None = None,
    expected_revision: FileRevision | None = None,
) -> FileObjectCandidateResult:
    nonce = secrets.token_hex(16)
    fixed_argv = build_helper_argv(plan, runtime_path=runtime_path, fixed_source=FIXED_SOURCE, nonce=nonce)
    root = _validate_text(trusted_root_path)
    leaf = _validate_text(relative_path)
    request_data = b""
    request_failure: FileObjectFailureCode | None = None
    try:
        request_data = encode_file_object_request(
            FileObjectRequest(
                nonce,
                operation,
                root,
                leaf,
                deadline.remaining(),
                plan.expected,
                expected_kind,
                expected_revision,
            )
        )
    except FileObjectRequestError as error:
        request_failure = error.failure
    if request_failure is FileObjectFailureCode.OVERSIZED_REQUEST:
        raise ValidationError("File-object request exceeds the 32768-byte manifest bound")
    if request_failure is not None:
        raise ValidationError("File-object request contains an invalid field")
    collector = _FileObjectCollector(operation)
    reader = FileRecordReader(nonce, collector.accept)
    stderr = _DiagnosticSink()
    io = CarrierIO(
        input=FiniteInput(request_data, sensitive=True),
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
        return FileObjectCandidateResult(
            report.dispatch,
            report.completion,
            report.local_status,
            report.failure,
            observation,
        )
    except BaseException as control:
        reader.abort()
        collector.abort()
        if operation is FileObjectOperation.REMOVE and dispatch is not Dispatch.NOT_SENT:
            raise control from FileObjectMutationUncertain()
        raise


def stat_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_path: str = _DEFAULT_RUNTIME,
) -> FileObjectCandidateResult:
    """Observe one supported object through one fresh helper attempt."""
    return _exchange(
        carrier,
        operation=FileObjectOperation.STAT,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        plan=plan,
        deadline=deadline,
        runtime_path=runtime_path,
    )


def remove_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    expected_kind: FileKind,
    expected_revision: FileRevision,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_path: str = _DEFAULT_RUNTIME,
) -> FileObjectCandidateResult:
    """Conditionally remove one object through one fresh non-replayed attempt."""
    return _exchange(
        carrier,
        operation=FileObjectOperation.REMOVE,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        plan=plan,
        deadline=deadline,
        runtime_path=runtime_path,
        expected_kind=expected_kind,
        expected_revision=expected_revision,
    )
