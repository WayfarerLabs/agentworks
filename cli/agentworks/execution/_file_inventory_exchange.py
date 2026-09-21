"""One-attempt host exchange for a bounded Linux directory inventory."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._file_inventory_bundle import FIXED_BUNDLE
from agentworks.execution._file_inventory_protocol import (
    FileInventoryControlError,
    FileInventoryFailureCode,
    FileInventoryRequest,
    FileInventoryRequestError,
    encode_file_inventory_request,
    parse_empty_file_inventory_body,
    parse_file_inventory_entries,
    parse_file_inventory_failure,
    parse_file_inventory_result,
)
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
    from agentworks.execution._file_inventory import FileInventoryEntry
    from agentworks.execution.carrier import Carrier, Deadline, ExitStatus

_DEFAULT_RUNTIME = "/usr/bin/python3"


class FileInventoryObservationState(StrEnum):
    PRESENT = "present"
    NOT_FOUND = "not_found"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"


class FileInventoryObservationError(StrEnum):
    CONTROL = "control"
    ORDER = "order"
    CONTENT = "content"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    MISSING_TERMINAL = "missing_terminal"


@dataclass(frozen=True, slots=True)
class FileInventoryObservation:
    state: FileInventoryObservationState
    entries: tuple[FileInventoryEntry, ...] | None = field(default=None, repr=False)
    failure: FileInventoryFailureCode | None = None
    error: FileInventoryObservationError | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class FileInventoryCandidateResult:
    """Separate raw carrier facts from one reduced typed inventory observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    observation: FileInventoryObservation


class _DiagnosticSink:
    """Reject but never retain any bytes outside the inventory protocol."""

    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        if data:
            self.saw_data = True
        return len(data)


class _FileInventoryCollector:
    """Reduce one concrete inventory transcript without exposing partial entries."""

    def __init__(self, *, max_entries: int, max_depth: int, max_encoded_bytes: int) -> None:
        self._max_entries = max_entries
        self._max_depth = max_depth
        self._max_encoded_bytes = max_encoded_bytes
        self._data = bytearray()
        self._entries: tuple[FileInventoryEntry, ...] | None = None
        self._not_found = False
        self._failure: FileInventoryFailureCode | None = None
        self._terminal = False
        self._error: FileInventoryObservationError | None = None

    def accept(self, record: FileRecord) -> None:
        if self._error is not None:
            return
        if self._terminal:
            self._fail(FileInventoryObservationError.POST_TERMINAL)
            return
        try:
            self._accept(record)
        except FileInventoryControlError:
            self._fail(FileInventoryObservationError.CONTROL)

    def _accept(self, record: FileRecord) -> None:
        if record.kind is FileRecordKind.DATA:
            if self._entries is not None or self._not_found or self._failure is not None:
                self._fail(FileInventoryObservationError.ORDER)
            elif len(self._data) + len(record.body) > self._max_encoded_bytes:
                self._fail(FileInventoryObservationError.CONTENT)
            else:
                self._data.extend(record.body)
            return
        if record.kind is FileRecordKind.RESULT:
            if self._entries is not None or self._not_found or self._failure is not None:
                self._fail(FileInventoryObservationError.ORDER)
                return
            result = parse_file_inventory_result(record.body, self._max_encoded_bytes)
            if result.length != len(self._data) or result.digest != hashlib.sha256(self._data).digest():
                self._fail(FileInventoryObservationError.CONTENT)
                return
            entries = parse_file_inventory_entries(
                bytes(self._data),
                max_entries=self._max_entries,
                max_depth=self._max_depth,
                max_encoded_bytes=self._max_encoded_bytes,
            )
            self._data.clear()
            self._entries = entries
            return
        if record.kind is FileRecordKind.ABSENT:
            parse_empty_file_inventory_body(record.body)
            if self._data or self._entries is not None or self._not_found or self._failure is not None:
                self._fail(FileInventoryObservationError.ORDER)
            else:
                self._not_found = True
            return
        if record.kind is FileRecordKind.FAILED:
            failure = parse_file_inventory_failure(record.body)
            if self._data or self._entries is not None or self._not_found or self._failure is not None:
                self._fail(FileInventoryObservationError.ORDER)
            else:
                self._failure = failure
            return
        if record.kind is FileRecordKind.FINISHED:
            parse_empty_file_inventory_body(record.body)
            outcomes = sum((self._entries is not None, self._not_found, self._failure is not None))
            if outcomes != 1:
                self._fail(FileInventoryObservationError.ORDER)
            else:
                self._terminal = True
            return
        self._fail(FileInventoryObservationError.ORDER)

    def _fail(self, error: FileInventoryObservationError) -> None:
        if self._error is None:
            self._error = error
        self._clear()

    def _clear(self) -> None:
        self._data.clear()
        self._entries = None
        self._not_found = False
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
    ) -> FileInventoryObservation:
        error: FileInventoryObservationError | FileWireError | None = self._error or wire_error
        state = FileInventoryObservationState.INVALID
        if error is None and stderr_noise:
            error = FileInventoryObservationError.STDERR
        if error is None and not streams_complete:
            error = FileInventoryObservationError.CARRIER
            state = FileInventoryObservationState.INCOMPLETE
        elif error is None and not self._terminal:
            error = FileInventoryObservationError.MISSING_TERMINAL
            state = FileInventoryObservationState.INCOMPLETE
        elif error is FileWireError.TRUNCATED:
            state = FileInventoryObservationState.INCOMPLETE
        if error is not None:
            self._clear()
            return FileInventoryObservation(state, error=error)
        if self._entries is not None:
            entries = self._entries
            self._clear()
            return FileInventoryObservation(FileInventoryObservationState.PRESENT, entries=entries)
        if self._not_found:
            self._clear()
            return FileInventoryObservation(FileInventoryObservationState.NOT_FOUND)
        assert self._failure is not None
        failure = self._failure
        self._clear()
        return FileInventoryObservation(FileInventoryObservationState.REFUSED, failure=failure)


def _validate_text(value: object) -> str:
    if type(value) is not str or "\0" in value:
        raise ValidationError("File-inventory paths must be valid non-NUL UTF-8 strings")
    failed = False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        failed = True
    if failed:
        raise ValidationError("File-inventory paths must be valid non-NUL UTF-8 strings")
    return value


def list_directory(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    max_entries: int,
    max_depth: int,
    max_encoded_bytes: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_path: str = _DEFAULT_RUNTIME,
) -> FileInventoryCandidateResult:
    """Inventory one confined directory through one fresh helper attempt."""
    nonce = secrets.token_hex(16)
    fixed_argv = build_helper_argv(
        plan,
        runtime_path=runtime_path,
        fixed_source=FIXED_BUNDLE.bootstrap,
        nonce=nonce,
    )
    root = _validate_text(trusted_root_path)
    relative = _validate_text(relative_path)
    request_data = b""
    request_failure: FileInventoryFailureCode | None = None
    try:
        request_data = encode_file_inventory_request(
            FileInventoryRequest(
                nonce,
                root,
                relative,
                max_entries,
                max_depth,
                max_encoded_bytes,
                plan.expected,
                deadline.remaining(),
            )
        )
    except FileInventoryRequestError as error:
        request_failure = error.failure
    if request_failure is FileInventoryFailureCode.OVERSIZED_REQUEST:
        raise ValidationError("File-inventory request exceeds the 32768-byte manifest bound")
    if request_failure is not None:
        raise ValidationError("File-inventory request contains an invalid field")
    collector = _FileInventoryCollector(
        max_entries=max_entries,
        max_depth=max_depth,
        max_encoded_bytes=max_encoded_bytes,
    )
    reader = FileRecordReader(nonce, collector.accept)
    stderr = _DiagnosticSink()
    io = CarrierIO(
        input=FiniteInput(FIXED_BUNDLE.prefix + request_data, sensitive=True),
        output=SinkOutput(reader, stderr, require_live=False),
        sensitive=True,
    )
    try:
        report = carrier.execute(PreparedInvocation(fixed_argv), io=io, deadline=deadline)
        reader.finish()
        delivered = report.stdout.retention is Retention.DELIVERED and report.stderr.retention is Retention.DELIVERED
        observation = collector.finish(
            reader.error,
            streams_complete=delivered and report.stdout.complete and report.stderr.complete,
            stderr_noise=stderr.saw_data,
        )
        return FileInventoryCandidateResult(
            report.dispatch,
            report.completion,
            report.local_status,
            report.failure,
            observation,
        )
    except BaseException:
        reader.abort()
        collector.abort()
        raise
