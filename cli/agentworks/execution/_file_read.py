"""Host dispatch and trusted observation for one private bounded file read."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._file_read_bundle import FIXED_BUNDLE
from agentworks.execution._file_read_protocol import (
    FileReadControlError,
    FileReadFailure,
    FileReadRequest,
    FileReadRequestError,
    FileReadResultControl,
    encode_file_read_request,
    parse_empty_file_read_body,
    parse_file_read_failure,
    parse_file_read_result,
)
from agentworks.execution._file_wire import (
    FileRecord,
    FileRecordKind,
    FileRecordReader,
    FileWireError,
)
from agentworks.execution._runtime_prerequisite import (
    RuntimePrefixSink,
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    build_runtime_identity_helper_argv,
)
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
    from agentworks.execution._file_stat import FileStat
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution.carrier import Carrier, Deadline, ExitStatus


class FileReadObservationState(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"


class FileReadObservationError(StrEnum):
    CONTROL = "control"
    ORDER = "order"
    CONTENT = "content"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    MISSING_TERMINAL = "missing_terminal"


@dataclass(frozen=True, slots=True, repr=False)
class FileReadSnapshot:
    data: bytes
    metadata: FileStat
    digest: bytes


@dataclass(frozen=True, slots=True)
class FileReadObservation:
    state: FileReadObservationState
    snapshot: FileReadSnapshot | None = field(default=None, repr=False)
    failure: FileReadFailure | None = None
    error: FileReadObservationError | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class FileReadCandidateResult:
    """Carrier facts and the separate typed helper observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: FileReadObservation | None


class _DiagnosticSink:
    """Reject but never retain any bytes outside the file protocol."""

    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        if data:
            self.saw_data = True
        return len(data)


class _FileReadCollector:
    """Validate the closed record grammar while retaining at most the caller bound."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._data = bytearray()
        self._result: FileReadResultControl | None = None
        self._absent = False
        self._failure: FileReadFailure | None = None
        self._terminal = False
        self._error: FileReadObservationError | None = None

    def accept(self, record: FileRecord) -> None:
        if self._error is not None:
            return
        if self._terminal:
            self._fail(FileReadObservationError.POST_TERMINAL)
            return
        try:
            self._accept(record)
        except FileReadControlError:
            self._fail(FileReadObservationError.CONTROL)

    def _accept(self, record: FileRecord) -> None:
        if record.kind is FileRecordKind.DATA:
            if self._result is not None or self._absent or self._failure is not None:
                self._fail(FileReadObservationError.ORDER)
            elif len(self._data) + len(record.body) > self._max_bytes:
                self._fail(FileReadObservationError.CONTENT)
            else:
                self._data.extend(record.body)
            return
        if record.kind is FileRecordKind.RESULT:
            if self._result is not None or self._absent or self._failure is not None:
                self._fail(FileReadObservationError.ORDER)
                return
            result = parse_file_read_result(record.body, self._max_bytes)
            if result.metadata.size != len(self._data) or result.digest != hashlib.sha256(self._data).digest():
                self._fail(FileReadObservationError.CONTENT)
            else:
                self._result = result
            return
        if record.kind is FileRecordKind.ABSENT:
            parse_empty_file_read_body(record.body)
            if self._data or self._result is not None or self._absent or self._failure is not None:
                self._fail(FileReadObservationError.ORDER)
            else:
                self._absent = True
            return
        if record.kind is FileRecordKind.FAILED:
            failure = parse_file_read_failure(record.body)
            if self._data or self._result is not None or self._absent or self._failure is not None:
                self._fail(FileReadObservationError.ORDER)
            else:
                self._failure = failure
            return
        if record.kind is FileRecordKind.FINISHED:
            parse_empty_file_read_body(record.body)
            if self._result is None and not self._absent and self._failure is None:
                self._fail(FileReadObservationError.ORDER)
            else:
                self._terminal = True
            return
        self._fail(FileReadObservationError.ORDER)

    def _fail(self, error: FileReadObservationError) -> None:
        if self._error is None:
            self._error = error
        self._clear()

    def _clear(self) -> None:
        self._data.clear()
        self._result = None
        self._absent = False
        self._failure = None
        self._terminal = False

    def abort(self) -> None:
        """Forget all transcript-derived state when the attempt cannot return a result."""
        self._clear()

    def finish(
        self,
        wire_error: FileWireError | None,
        *,
        streams_complete: bool,
        stderr_noise: bool,
    ) -> FileReadObservation:
        error: FileReadObservationError | FileWireError | None = self._error or wire_error
        state = FileReadObservationState.INVALID
        if error is None and stderr_noise:
            error = FileReadObservationError.STDERR
        if error is None and not streams_complete:
            error = FileReadObservationError.CARRIER
            state = FileReadObservationState.INCOMPLETE
        elif error is None and not self._terminal:
            error = FileReadObservationError.MISSING_TERMINAL
            state = FileReadObservationState.INCOMPLETE
        elif error is FileWireError.TRUNCATED:
            state = FileReadObservationState.INCOMPLETE
        if error is not None:
            self._clear()
            return FileReadObservation(state=state, error=error)
        if self._result is not None:
            snapshot = FileReadSnapshot(bytes(self._data), self._result.metadata, self._result.digest)
            self._clear()
            return FileReadObservation(state=FileReadObservationState.PRESENT, snapshot=snapshot)
        if self._absent:
            self._clear()
            return FileReadObservation(state=FileReadObservationState.ABSENT)
        assert self._failure is not None
        failure = self._failure
        self._clear()
        return FileReadObservation(state=FileReadObservationState.REFUSED, failure=failure)


def _validate_text(value: object) -> str:
    if type(value) is not str or "\0" in value:
        raise ValidationError("File-read paths must be valid non-NUL UTF-8 strings")
    failed = False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        failed = True
    if failed:
        raise ValidationError("File-read paths must be valid non-NUL UTF-8 strings")
    return value


def read_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    max_bytes: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FileReadCandidateResult:
    """Validate and dispatch one bounded read without replay or target writes."""
    nonce = secrets.token_hex(16)
    fixed_argv, candidates, system_shim = build_runtime_identity_helper_argv(
        plan,
        selection=runtime_selection,
        fixed_source=FIXED_BUNDLE.bootstrap,
        nonce=nonce,
    )
    root = _validate_text(trusted_root_path)
    leaf = _validate_text(relative_path)
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValidationError("File-read byte bound must be a positive integer")
    request_failure: FileReadFailure | None = None
    try:
        request_data = encode_file_read_request(
            FileReadRequest(nonce, root, leaf, max_bytes, plan.expected, deadline.remaining())
        )
    except FileReadRequestError as error:
        request_failure = error.failure
    if request_failure is FileReadFailure.OVERSIZED_REQUEST:
        raise ValidationError("File-read request exceeds the 32768-byte manifest bound")
    if request_failure is not None:
        raise ValidationError("File-read request contains an invalid field")
    collector = _FileReadCollector(max_bytes)
    reader = FileRecordReader(nonce, collector.accept)
    runtime = RuntimePrefixSink(nonce, candidates, reader, system_shim)
    stderr = _DiagnosticSink()
    invocation = PreparedInvocation(fixed_argv)
    io = CarrierIO(
        input=FiniteInput(FIXED_BUNDLE.prefix + request_data, sensitive=True),
        output=SinkOutput(runtime, stderr, require_live=False),
        sensitive=True,
    )
    try:
        report = carrier.execute(invocation, io=io, deadline=deadline)
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
            )
        else:
            reader.abort()
            collector.abort()
            observation = None
        return FileReadCandidateResult(
            dispatch=report.dispatch,
            carrier_completion=report.completion,
            carrier_local_status=report.local_status,
            carrier_failure=report.failure,
            runtime_prerequisite=runtime_prerequisite,
            observation=observation,
        )
    except BaseException:
        reader.abort()
        collector.abort()
        raise
    finally:
        runtime.clear()
