"""One-attempt host exchanges for Linux file metadata operations."""

from __future__ import annotations

import secrets
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._file_metadata import MetadataEffect, MetadataPhase, MetadataStep
from agentworks.execution._file_metadata_bundle import FIXED_BUNDLE
from agentworks.execution._file_metadata_protocol import (
    FileMetadataControlError,
    FileMetadataFailureCode,
    FileMetadataFailureControl,
    FileMetadataOperation,
    FileMetadataRequest,
    FileMetadataRequestError,
    FileMetadataResultControl,
    encode_file_metadata_request,
    parse_empty_file_metadata_body,
    parse_file_metadata_failure,
    parse_file_metadata_result,
)
from agentworks.execution._file_wire import FileRecord, FileRecordKind, FileRecordReader, FileWireError
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
    from agentworks.execution._file_stat import FileRevision
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution.carrier import Carrier, Deadline, ExitStatus


class FileMetadataObservationState(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    REFUSED = "refused"
    PARTIAL = "partial"
    UNCERTAIN = "uncertain"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"


class FileMetadataObservationError(StrEnum):
    CONTROL = "control"
    ORDER = "order"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    MISSING_TERMINAL = "missing_terminal"


@dataclass(frozen=True, slots=True)
class FileMetadataObservation:
    state: FileMetadataObservationState
    revision: FileRevision | None = field(default=None, repr=False)
    failure: FileMetadataFailureControl | None = None
    error: FileMetadataObservationError | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class FileMetadataCandidateResult:
    """Separate raw carrier facts from one reduced typed helper observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: FileMetadataObservation | None


class FileMetadataMutationUncertain(Exception):
    """Safe cause retained when control escapes a possibly dispatched mutation."""

    def __init__(self) -> None:
        super().__init__("file metadata mutation may have been dispatched")


class _DiagnosticSink:
    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        if data:
            self.saw_data = True
        return len(data)

    def clear(self) -> None:
        self.saw_data = False


class _FileMetadataCollector:
    """Reduce the operation-specific grammar without retaining raw records."""

    def __init__(self, operation: FileMetadataOperation) -> None:
        self._operation = operation
        self._result: FileMetadataResultControl | None = None
        self._failure: FileMetadataFailureControl | None = None
        self._terminal = False
        self._error: FileMetadataObservationError | None = None

    def accept(self, record: FileRecord) -> None:
        if self._error is not None:
            return
        if self._terminal:
            self._fail(FileMetadataObservationError.POST_TERMINAL)
            return
        try:
            if record.kind is FileRecordKind.RESULT:
                if self._result is not None or self._failure is not None:
                    self._fail(FileMetadataObservationError.ORDER)
                else:
                    result = parse_file_metadata_result(record.body)
                    if self._operation is FileMetadataOperation.ENSURE_DIRECTORY and not stat.S_ISDIR(
                        result.revision.stat.mode
                    ):
                        self._fail(FileMetadataObservationError.CONTROL)
                    else:
                        self._result = result
            elif record.kind is FileRecordKind.FAILED:
                if self._result is not None or self._failure is not None:
                    self._fail(FileMetadataObservationError.ORDER)
                else:
                    failure = parse_file_metadata_failure(record.body)
                    if not self._valid_failure(failure):
                        self._fail(FileMetadataObservationError.CONTROL)
                    else:
                        self._failure = failure
            elif record.kind is FileRecordKind.FINISHED:
                parse_empty_file_metadata_body(record.body)
                if (self._result is None) == (self._failure is None):
                    self._fail(FileMetadataObservationError.ORDER)
                else:
                    self._terminal = True
            else:
                self._fail(FileMetadataObservationError.ORDER)
        except FileMetadataControlError:
            self._fail(FileMetadataObservationError.CONTROL)

    def _valid_failure(self, failure: FileMetadataFailureControl) -> bool:
        if failure.code is not FileMetadataFailureCode.METADATA:
            return True
        if self._operation is FileMetadataOperation.SET_METADATA:
            return (
                failure.phase is not MetadataPhase.CREATION
                and MetadataStep.CREATION not in failure.completed_steps
                and failure.attempted_step is not MetadataStep.CREATION
            )
        return True

    def _fail(self, error: FileMetadataObservationError) -> None:
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
    ) -> FileMetadataObservation:
        error: FileMetadataObservationError | FileWireError | None = self._error or wire_error
        state = FileMetadataObservationState.INVALID
        if error is None and stderr_noise:
            error = FileMetadataObservationError.STDERR
        if error is None and not streams_complete:
            error = FileMetadataObservationError.CARRIER
            state = FileMetadataObservationState.INCOMPLETE
        elif error is None and not self._terminal:
            error = FileMetadataObservationError.MISSING_TERMINAL
            state = FileMetadataObservationState.INCOMPLETE
        elif error is FileWireError.TRUNCATED:
            state = FileMetadataObservationState.INCOMPLETE
        if error is not None:
            self._clear()
            if dispatch is not Dispatch.NOT_SENT:
                state = FileMetadataObservationState.UNCERTAIN
            return FileMetadataObservation(state, error=error)
        if self._failure is not None:
            failure = self._failure
            self._clear()
            state = {
                MetadataEffect.UNCHANGED: FileMetadataObservationState.REFUSED,
                MetadataEffect.PARTIAL: FileMetadataObservationState.PARTIAL,
                MetadataEffect.UNCERTAIN: FileMetadataObservationState.UNCERTAIN,
                None: FileMetadataObservationState.REFUSED,
            }[failure.effect]
            return FileMetadataObservation(state, failure=failure)
        assert self._result is not None
        result = self._result
        self._clear()
        return FileMetadataObservation(
            FileMetadataObservationState(result.kind.value),
            revision=result.revision,
        )


def _validate_text(value: object) -> str:
    if type(value) is not str or "\0" in value:
        raise ValidationError("File metadata paths must be valid non-NUL UTF-8 strings")
    failed = False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        failed = True
    if failed:
        raise ValidationError("File metadata paths must be valid non-NUL UTF-8 strings")
    return value


def _exchange(
    carrier: Carrier,
    *,
    operation: FileMetadataOperation,
    trusted_root_path: str,
    relative_path: str,
    uid: int,
    gid: int,
    mode: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FileMetadataCandidateResult:
    nonce = secrets.token_hex(16)
    fixed_argv, candidates, system_shim = build_runtime_identity_helper_argv(
        plan,
        selection=runtime_selection,
        fixed_source=FIXED_BUNDLE.bootstrap,
        nonce=nonce,
    )
    root = _validate_text(trusted_root_path)
    leaf = _validate_text(relative_path)
    request_data = b""
    request_failure: FileMetadataFailureCode | None = None
    try:
        request_data = encode_file_metadata_request(
            FileMetadataRequest(
                nonce,
                operation,
                root,
                leaf,
                uid,
                gid,
                mode,
                deadline.remaining(),
                plan.expected,
            )
        )
    except FileMetadataRequestError as error:
        request_failure = error.failure
    if request_failure is FileMetadataFailureCode.OVERSIZED_REQUEST:
        raise ValidationError("File metadata request exceeds the 32768-byte manifest bound")
    if request_failure is not None:
        raise ValidationError("File metadata request contains an invalid field")
    collector = _FileMetadataCollector(operation)
    reader = FileRecordReader(nonce, collector.accept)
    runtime = RuntimePrefixSink(nonce, candidates, reader, system_shim)
    stderr = _DiagnosticSink()
    io = CarrierIO(
        input=FiniteInput(FIXED_BUNDLE.prefix + request_data, sensitive=True),
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
        return FileMetadataCandidateResult(
            report.dispatch,
            report.completion,
            report.local_status,
            report.failure,
            runtime_prerequisite,
            observation,
        )
    except BaseException as control:
        if dispatch is not Dispatch.NOT_SENT:
            try:
                fact = FileMetadataMutationUncertain()
            except BaseException:
                raise control from None
            raise control from fact
        raise
    finally:
        runtime.clear()
        reader.abort()
        collector.abort()
        stderr.clear()


def set_file_metadata(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    uid: int,
    gid: int,
    mode: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FileMetadataCandidateResult:
    """Converge one object's metadata through one fresh non-replayed attempt."""
    return _exchange(
        carrier,
        operation=FileMetadataOperation.SET_METADATA,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        uid=uid,
        gid=gid,
        mode=mode,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
    )


def ensure_file_directory(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    uid: int,
    gid: int,
    mode: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> FileMetadataCandidateResult:
    """Create or converge one final directory through one fresh attempt."""
    return _exchange(
        carrier,
        operation=FileMetadataOperation.ENSURE_DIRECTORY,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        uid=uid,
        gid=gid,
        mode=mode,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
    )
