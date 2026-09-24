"""One-attempt carrier exchange for private managed facts and closed output."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from . import _managed_job_wire as wire
from ._file_wire import MAX_RECORD_BODY_BYTES, FileRecord, FileRecordKind, FileRecordReader, FileWireError
from ._managed_job_store import FactName, Stream
from ._managed_observation_bundle import FIXED_BUNDLE
from ._managed_observation_protocol import (
    FACT_ORDER,
    ManagedObservationError,
    ManagedObservationRequest,
    ManagedOperation,
    ManagedResultControl,
    ManagedResultStatus,
    checked_fact,
    checked_launch,
    decode_result,
    encode_request,
)
from ._runtime_prerequisite import (
    RuntimePrefixSink,
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
    build_runtime_identity_helper_argv,
)
from .carrier import CarrierIO, Dispatch, Failure, FiniteInput, PreparedInvocation, Retention, SinkOutput

_MAX_RESPONSE_RECORDS = (
    2 + len(FACT_ORDER) + (wire.MAX_CAPTURE_PREFIX_BYTES_V1 + MAX_RECORD_BODY_BYTES - 1) // MAX_RECORD_BODY_BYTES
)

if TYPE_CHECKING:
    from ._helper_launcher import IdentityPlan
    from .carrier import Carrier, Deadline, ExitStatus


class ManagedObservationState(StrEnum):
    OBSERVED = "observed"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class ManagedObservationIssue(StrEnum):
    ORDER = "order"
    CONTROL = "control"
    CONTENT = "content"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    COMPLETION = "completion"
    MISSING_TERMINAL = "missing_terminal"


@dataclass(frozen=True, slots=True)
class ManagedObservation:
    state: ManagedObservationState
    facts: tuple[tuple[FactName, bytes], ...] = field(default=(), repr=False)
    output: bytes | None = field(default=None, repr=False)
    issue: ManagedObservationIssue | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class ManagedObservationCandidate:
    """Raw carrier evidence and separately reduced helper observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: ManagedObservation | None


class _DiagnosticSink:
    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        self.saw_data |= bool(data)
        return len(data)

    def clear(self) -> None:
        self.saw_data = False


class _Collector:
    def __init__(self, request: ManagedObservationRequest) -> None:
        self.request = request
        self.control: ManagedResultControl | None = None
        self.failed = False
        self.terminal = False
        self.issue: ManagedObservationIssue | None = None
        self.facts: list[tuple[FactName, bytes]] = []
        self.output = bytearray()
        self.records = 0

    def _invalidate(self, issue: ManagedObservationIssue) -> None:
        if self.issue is None:
            self.issue = issue
        self.abort()

    def abort(self) -> None:
        self.facts.clear()
        self.output.clear()
        self.control = None

    def accept(self, record: FileRecord) -> None:
        if self.issue is not None:
            return
        self.records += 1
        if self.records > _MAX_RESPONSE_RECORDS:
            self._invalidate(ManagedObservationIssue.CONTENT)
            return
        if self.terminal:
            self._invalidate(ManagedObservationIssue.POST_TERMINAL)
            return
        if record.kind is FileRecordKind.FAILED and self.control is None and not self.failed:
            if record.body:
                self._invalidate(ManagedObservationIssue.CONTROL)
            else:
                self.failed = True
            return
        if record.kind is FileRecordKind.RESULT and self.control is None and not self.failed:
            try:
                control = decode_result(record.body)
                if self.request.operation is ManagedOperation.OBSERVE:
                    if control.status is not ManagedResultStatus.OBSERVED or control.output_bytes:
                        raise ManagedObservationError("invalid observe result")
                elif control.status is ManagedResultStatus.OBSERVED:
                    raise ManagedObservationError("invalid output result")
                elif control.status is ManagedResultStatus.UNKNOWN:
                    if control.facts != (FactName.LAUNCH,):
                        raise ManagedObservationError("invalid unknown result")
                elif control.facts != (
                    FactName.LAUNCH,
                    FactName.STDOUT_END if self.request.stream is Stream.STDOUT else FactName.STDERR_END,
                ):
                    raise ManagedObservationError("invalid capture result")
                self.control = control
            except ManagedObservationError:
                self._invalidate(ManagedObservationIssue.CONTROL)
            return
        if record.kind is FileRecordKind.DATA and self.control is not None:
            if len(self.facts) < len(self.control.facts):
                name = self.control.facts[len(self.facts)]
                try:
                    checked_fact(name, record.body, self.request.expected_launch)
                except ManagedObservationError:
                    self._invalidate(ManagedObservationIssue.CONTENT)
                    return
                self.facts.append((name, record.body))
            elif self.control.status is ManagedResultStatus.AVAILABLE:
                if not record.body or len(record.body) > self.control.output_bytes - len(self.output):
                    self._invalidate(ManagedObservationIssue.CONTENT)
                else:
                    self.output.extend(record.body)
            else:
                self._invalidate(ManagedObservationIssue.ORDER)
            return
        if record.kind is FileRecordKind.FINISHED:
            if record.body or (self.control is None) == (not self.failed):
                self._invalidate(ManagedObservationIssue.ORDER)
            else:
                self.terminal = True
            return
        self._invalidate(ManagedObservationIssue.ORDER)

    def finish(
        self,
        wire_error: FileWireError | None,
        *,
        complete: bool,
        stderr_noise: bool,
        dispatch: Dispatch,
        completion: ExitStatus | None,
        carrier_failure: Failure | None,
    ) -> ManagedObservation:
        issue: ManagedObservationIssue | FileWireError | None = self.issue or wire_error
        if issue is None and stderr_noise:
            issue = ManagedObservationIssue.STDERR
        if issue is None and not complete:
            issue = ManagedObservationIssue.CARRIER
        if issue is None and (carrier_failure is not None or completion is None or completion.code != 0):
            issue = ManagedObservationIssue.COMPLETION
        if issue is None and not self.terminal:
            issue = ManagedObservationIssue.MISSING_TERMINAL
        if dispatch is not Dispatch.SENT:
            self.abort()
            return ManagedObservation(
                ManagedObservationState.UNKNOWN if dispatch is Dispatch.UNKNOWN else ManagedObservationState.INCOMPLETE,
                issue=issue,
            )
        if issue is not None:
            self.abort()
            return ManagedObservation(
                ManagedObservationState.INCOMPLETE
                if issue
                in (ManagedObservationIssue.CARRIER, ManagedObservationIssue.MISSING_TERMINAL, FileWireError.TRUNCATED)
                else ManagedObservationState.INVALID,
                issue=issue,
            )
        if self.failed:
            self.abort()
            return ManagedObservation(ManagedObservationState.REFUSED)
        assert self.control is not None
        control = self.control
        if len(self.facts) != len(control.facts) or len(self.output) != control.output_bytes:
            self.abort()
            return ManagedObservation(ManagedObservationState.INCOMPLETE, issue=ManagedObservationIssue.CONTENT)
        if control.status in (ManagedResultStatus.AVAILABLE, ManagedResultStatus.UNAVAILABLE):
            end = checked_fact(self.facts[1][0], self.facts[1][1], self.request.expected_launch)
            valid_capture = (
                end["disposition"] in ("complete-capture", "truncated-capture")
                and end["retained_bytes"] == len(self.output)
                and end["retained_sha256"] == hashlib.sha256(self.output).hexdigest()
            )
            valid_unavailable = end["disposition"] in ("discarded", "sensitivity-suppressed")
            if (control.status is ManagedResultStatus.AVAILABLE and not valid_capture) or (
                control.status is ManagedResultStatus.UNAVAILABLE and not valid_unavailable
            ):
                self.abort()
                return ManagedObservation(ManagedObservationState.INVALID, issue=ManagedObservationIssue.CONTENT)
        facts = tuple(self.facts)
        output = bytes(self.output) if control.status is ManagedResultStatus.AVAILABLE else None
        state = ManagedObservationState(control.status.value)
        self.abort()
        return ManagedObservation(state, facts, output)


def _exchange(
    carrier: Carrier,
    *,
    operation: ManagedOperation,
    expected_launch: bytes,
    stream: Stream | None,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> ManagedObservationCandidate:
    if runtime_selection.target_os is not RuntimeTargetOS.LINUX or plan.expected.euid != 0:
        raise ValidationError("Managed observation requires a Linux root helper")
    try:
        checked_launch(expected_launch)
        request = ManagedObservationRequest(secrets.token_hex(16), operation, expected_launch, plan.expected, stream)
        request_data = encode_request(request)
    except ManagedObservationError:
        raise ValidationError("Invalid managed observation request") from None
    if deadline.expired:
        return ManagedObservationCandidate(
            Dispatch.NOT_SENT,
            None,
            None,
            Failure.DEADLINE,
            RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None),
            None,
        )
    argv, candidates, shim = build_runtime_identity_helper_argv(
        plan, selection=runtime_selection, fixed_source=FIXED_BUNDLE.bootstrap, nonce=request.nonce
    )
    collector = _Collector(request)
    reader = FileRecordReader(request.nonce, collector.accept)
    runtime = RuntimePrefixSink(request.nonce, candidates, reader, shim)
    stderr = _DiagnosticSink()
    io = CarrierIO(
        input=FiniteInput(FIXED_BUNDLE.prefix + request_data, sensitive=True),
        output=SinkOutput(runtime, stderr, require_live=False),
        sensitive=True,
    )
    try:
        report = carrier.execute(PreparedInvocation(argv), io=io, deadline=deadline)
        prerequisite = runtime.observation
        if prerequisite.state is RuntimePrerequisiteState.READY:
            reader.finish()
            complete = (
                report.stdout.retention is Retention.DELIVERED
                and report.stderr.retention is Retention.DELIVERED
                and report.stdout.complete
                and report.stderr.complete
            )
            observation = collector.finish(
                reader.error,
                complete=complete,
                stderr_noise=stderr.saw_data,
                dispatch=report.dispatch,
                completion=report.completion,
                carrier_failure=report.failure,
            )
        else:
            observation = None
        return ManagedObservationCandidate(
            report.dispatch, report.completion, report.local_status, report.failure, prerequisite, observation
        )
    finally:
        runtime.clear()
        reader.abort()
        collector.abort()
        stderr.clear()


def observe_managed_run(
    carrier: Carrier,
    *,
    expected_launch: bytes,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> ManagedObservationCandidate:
    """Read present fixed facts for one expected run; absence stays unknown."""
    return _exchange(
        carrier,
        operation=ManagedOperation.OBSERVE,
        expected_launch=expected_launch,
        stream=None,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
    )


def read_managed_output(
    carrier: Carrier,
    *,
    expected_launch: bytes,
    stream: Stream,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> ManagedObservationCandidate:
    """Read only one closed captured stream for the expected run."""
    return _exchange(
        carrier,
        operation=ManagedOperation.READ_OUTPUT,
        expected_launch=expected_launch,
        stream=stream,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
    )
