"""Durably one-shot private managed start over one carrier attempt."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from . import _managed_job_request as request_wire
from ._file_wire import FileRecord, FileRecordKind, FileRecordReader, FileWireError
from ._helper_launcher import IdentityPlan
from ._managed_job_protocol import ManagedJobFactError, encode_managed_job_fact
from ._managed_job_store import FactName
from ._managed_observation_protocol import ManagedObservationError, checked_fact
from ._managed_runs import (
    ManagedLaunchObservation,
    ManagedLaunchState,
    ManagedRunLifetime,
    ManagedRunReceipt,
    ManagedRunRecord,
    ManagedRunRepository,
    launch_managed_run,
)
from ._managed_start_bundle import FIXED_BUNDLE
from ._managed_start_protocol import (
    ManagedStartError,
    ManagedStartRequest,
    ManagedStartResult,
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
from .carrier import CarrierIO, Deadline, Dispatch, Failure, FiniteInput, PreparedInvocation, Retention, SinkOutput

if TYPE_CHECKING:
    from .carrier import Carrier, ExitStatus


class ManagedStartState(StrEnum):
    ACKNOWLEDGED = "acknowledged"
    UNCERTAIN = "uncertain"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"


class ManagedStartIssue(StrEnum):
    ORDER = "order"
    CONTROL = "control"
    CONTENT = "content"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    COMPLETION = "completion"
    MISSING_TERMINAL = "missing_terminal"


@dataclass(frozen=True, slots=True)
class ManagedStartObservation:
    state: ManagedStartState
    systemd_exit_code: int | None = None
    systemd_signal: int | None = None
    launch_fact: bytes | None = field(default=None, repr=False)
    issue: ManagedStartIssue | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class ManagedStartCandidate:
    """Raw carrier evidence and separately admitted helper/systemd evidence."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: ManagedStartObservation | None


@dataclass(frozen=True, slots=True)
class ManagedStartAttempt:
    record: ManagedRunRecord
    candidate: ManagedStartCandidate


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedAttempt:
    nonce: str
    invocation: PreparedInvocation
    io: CarrierIO
    collector: _Collector
    reader: FileRecordReader
    runtime: RuntimePrefixSink
    stderr: _DiagnosticSink
    expected_launch: bytes


class _DiagnosticSink:
    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        self.saw_data |= bool(data)
        return len(data)

    def clear(self) -> None:
        self.saw_data = False


class _Collector:
    def __init__(self, expected_launch: bytes) -> None:
        self.expected_launch = expected_launch
        self.control: ManagedStartResult | None = None
        self.launch: bytes | None = None
        self.failed = False
        self.terminal = False
        self.issue: ManagedStartIssue | None = None
        self.records = 0

    def _invalidate(self, issue: ManagedStartIssue) -> None:
        if self.issue is None:
            self.issue = issue
        self.abort()

    def abort(self) -> None:
        self.control = None
        self.launch = None

    def accept(self, record: FileRecord) -> None:
        if self.issue is not None:
            return
        self.records += 1
        if self.records > 3:
            self._invalidate(ManagedStartIssue.CONTENT)
            return
        if self.terminal:
            self._invalidate(ManagedStartIssue.POST_TERMINAL)
            return
        if record.kind is FileRecordKind.FAILED and self.control is None and not self.failed:
            if record.body:
                self._invalidate(ManagedStartIssue.CONTROL)
            else:
                self.failed = True
            return
        if record.kind is FileRecordKind.RESULT and self.control is None and not self.failed:
            try:
                self.control = decode_result(record.body)
            except ManagedStartError:
                self._invalidate(ManagedStartIssue.CONTROL)
            return
        if record.kind is FileRecordKind.DATA and self.control is not None and self.control.facts == (FactName.LAUNCH,):
            if self.launch is not None:
                self._invalidate(ManagedStartIssue.ORDER)
                return
            try:
                checked_fact(FactName.LAUNCH, record.body, self.expected_launch)
            except ManagedObservationError:
                self._invalidate(ManagedStartIssue.CONTENT)
                return
            self.launch = record.body
            return
        if record.kind is FileRecordKind.FINISHED:
            if record.body or (self.control is None) == (not self.failed):
                self._invalidate(ManagedStartIssue.ORDER)
            else:
                self.terminal = True
            return
        self._invalidate(ManagedStartIssue.ORDER)

    def finish(
        self,
        wire_error: FileWireError | None,
        *,
        complete: bool,
        stderr_noise: bool,
        dispatch: Dispatch,
        completion: ExitStatus | None,
        carrier_failure: Failure | None,
    ) -> ManagedStartObservation:
        issue: ManagedStartIssue | FileWireError | None = self.issue or wire_error
        if issue is None and stderr_noise:
            issue = ManagedStartIssue.STDERR
        if issue is None and not complete:
            issue = ManagedStartIssue.CARRIER
        if issue is None and (carrier_failure is not None or completion is None or completion.code != 0):
            issue = ManagedStartIssue.COMPLETION
        if issue is None and not self.terminal:
            issue = ManagedStartIssue.MISSING_TERMINAL
        if dispatch is not Dispatch.SENT:
            self.abort()
            return ManagedStartObservation(ManagedStartState.INCOMPLETE, issue=issue)
        if issue is not None:
            self.abort()
            return ManagedStartObservation(
                ManagedStartState.INCOMPLETE
                if issue in (ManagedStartIssue.CARRIER, ManagedStartIssue.MISSING_TERMINAL, FileWireError.TRUNCATED)
                else ManagedStartState.INVALID,
                issue=issue,
            )
        if self.failed:
            self.abort()
            return ManagedStartObservation(ManagedStartState.REFUSED)
        assert self.control is not None
        control = self.control
        if (control.facts == (FactName.LAUNCH,)) != (self.launch is not None):
            self.abort()
            return ManagedStartObservation(ManagedStartState.INCOMPLETE, issue=ManagedStartIssue.CONTENT)
        if control.exit_code == 0 and self.launch is None:
            self.abort()
            return ManagedStartObservation(ManagedStartState.INVALID, issue=ManagedStartIssue.CONTENT)
        state = ManagedStartState.ACKNOWLEDGED if control.exit_code == 0 else ManagedStartState.UNCERTAIN
        observation = ManagedStartObservation(state, control.exit_code, control.signal, self.launch)
        self.abort()
        return observation


def _prepare_attempt(
    carrier: Carrier,
    reserved: ManagedRunRecord,
    request: request_wire.ManagedJobRequest,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> _PreparedAttempt:
    """Validate all caller input before the durable possible-dispatch transition."""
    if type(reserved) is not ManagedRunRecord or reserved.launch_state is not ManagedLaunchState.RESERVED:
        raise ValidationError("Managed start requires an exact reserved run")
    if reserved.spec.lifetime is not ManagedRunLifetime.INDEPENDENT:
        raise ValidationError("Managed start supports only independent lifetime")
    if type(plan) is not IdentityPlan or type(deadline) is not Deadline:
        raise ValidationError("Managed start requires a bound identity and deadline")
    if (
        type(runtime_selection) is not RuntimeSelection
        or runtime_selection.target_os is not RuntimeTargetOS.LINUX
        or plan.expected.euid != 0
    ):
        raise ValidationError("Managed start requires a Linux root helper")
    if deadline.expired:
        raise ValidationError("Managed start deadline has expired")
    try:
        expected_launch = encode_managed_job_fact(
            ManagedRunReceipt(reserved.identity, reserved.identity.unit_name, reserved.spec)
        )
        assets = request_wire.encode_request(request)
        if assets["request-launch"] != expected_launch:
            raise ValidationError("Managed start request does not match reservation")
        policy = reserved.output_policy
        if request.output_mode != policy.mode.value or request.capture_prefix_bytes != policy.capture_prefix_bytes:
            raise ValidationError("Managed start output policy mismatch")
        nonce = secrets.token_hex(16)
        request_data = encode_request(ManagedStartRequest(nonce, plan.expected, request))
        argv, candidates, shim = build_runtime_identity_helper_argv(
            plan, selection=runtime_selection, fixed_source=FIXED_BUNDLE.bootstrap, nonce=nonce
        )
    except (ManagedJobFactError, request_wire.RequestError, ManagedStartError):
        raise ValidationError("Invalid managed start request") from None
    collector = _Collector(expected_launch)
    reader = FileRecordReader(nonce, collector.accept)
    runtime = RuntimePrefixSink(nonce, candidates, reader, shim)
    stderr = _DiagnosticSink()
    invocation = PreparedInvocation(argv)
    io = CarrierIO(
        input=FiniteInput(FIXED_BUNDLE.prefix + request_data, sensitive=True),
        output=SinkOutput(runtime, stderr, require_live=False),
        sensitive=True,
    )
    carrier.validate(invocation, io=io)
    return _PreparedAttempt(nonce, invocation, io, collector, reader, runtime, stderr, expected_launch)


def _exchange(carrier: Carrier, prepared: _PreparedAttempt, deadline: Deadline) -> ManagedStartCandidate:
    if deadline.expired:
        return ManagedStartCandidate(
            Dispatch.NOT_SENT,
            None,
            None,
            Failure.DEADLINE,
            RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None),
            None,
        )
    try:
        report = carrier.execute(prepared.invocation, io=prepared.io, deadline=deadline)
        prerequisite = prepared.runtime.observation
        observation = None
        if prerequisite.state is RuntimePrerequisiteState.READY:
            prepared.reader.finish()
            complete = (
                report.stdout.retention is Retention.DELIVERED
                and report.stderr.retention is Retention.DELIVERED
                and report.stdout.complete
                and report.stderr.complete
            )
            observation = prepared.collector.finish(
                prepared.reader.error,
                complete=complete,
                stderr_noise=prepared.stderr.saw_data,
                dispatch=report.dispatch,
                completion=report.completion,
                carrier_failure=report.failure,
            )
        return ManagedStartCandidate(
            report.dispatch, report.completion, report.local_status, report.failure, prerequisite, observation
        )
    finally:
        prepared.runtime.clear()
        prepared.reader.abort()
        prepared.collector.abort()
        prepared.stderr.clear()


def start_managed_run(
    repository: ManagedRunRepository,
    reserved: ManagedRunRecord,
    carrier: Carrier,
    *,
    request: request_wire.ManagedJobRequest,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> ManagedStartAttempt:
    """Commit possible dispatch, attempt once, and reconcile only admitted launch fact."""
    prepared = _prepare_attempt(carrier, reserved, request, plan, deadline, runtime_selection)
    candidate: ManagedStartCandidate | None = None

    def boundary(run: ManagedRunRecord) -> ManagedLaunchObservation:
        nonlocal candidate
        assert run.identity == reserved.identity
        candidate = _exchange(carrier, prepared, deadline)
        observed = candidate.observation
        receipt = (
            ManagedRunReceipt(reserved.identity, reserved.identity.unit_name, reserved.spec)
            if observed is not None and observed.launch_fact == prepared.expected_launch
            else None
        )
        return ManagedLaunchObservation(candidate.dispatch, receipt)

    record = launch_managed_run(repository, reserved, boundary)
    assert candidate is not None
    return ManagedStartAttempt(record, candidate)
