"""Separate fixed-helper deliveries for operation clock and lease control."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError

from ._delivery_custody import LocalDeliveryCustody
from ._file_wire import FileRecord, FileRecordKind
from ._file_wire_reader import FileRecordReader, FileWireError
from ._helper_launcher import _validate_plan
from ._managed_lease_bundle import FIXED_BUNDLE
from ._managed_lease_protocol import (
    ClockObservation,
    LeasePublication,
    LeaseRequest,
    decode_result,
    encode_request,
)
from ._managed_lease_wire import LeaseError, OperationLease
from ._runtime_prerequisite import (
    RuntimePrefixSink,
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
    build_runtime_identity_helper_argv,
)
from ._vm_guest_identity_protocol import VMGuestIdentity
from .carrier import CarrierIO, Dispatch, Failure, FiniteInput, PreparedInvocation, Retention, SinkOutput

if TYPE_CHECKING:
    from ._helper_launcher import IdentityPlan
    from .carrier import Carrier, Deadline, ExitStatus


class ManagedLeaseState(StrEnum):
    OBSERVED = "observed"
    PUBLISHED = "published"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class ManagedLeaseIssue(StrEnum):
    ORDER = "order"
    CONTROL = "control"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    COMPLETION = "completion"
    CUSTODY = "custody"
    DEADLINE = "deadline"
    MISSING_TERMINAL = "missing_terminal"


@dataclass(frozen=True, slots=True)
class ManagedLeaseObservation:
    state: ManagedLeaseState
    result: ClockObservation | LeasePublication | None = field(default=None, repr=False)
    issue: ManagedLeaseIssue | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class ManagedLeaseCandidate:
    """Carrier evidence remains independent of the accepted helper result."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: ManagedLeaseObservation | None


class _DiagnosticSink:
    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        self.saw_data |= bool(data)
        return len(data)

    def clear(self) -> None:
        self.saw_data = False


class _Collector:
    def __init__(self, request: LeaseRequest) -> None:
        self.request = request
        self.result: ClockObservation | LeasePublication | None = None
        self.failed = False
        self.terminal = False
        self.issue: ManagedLeaseIssue | None = None

    def abort(self) -> None:
        self.result = None

    def _invalidate(self, issue: ManagedLeaseIssue) -> None:
        self.issue = issue
        self.abort()

    def accept(self, record: FileRecord) -> None:
        if self.issue is not None:
            return
        if self.terminal:
            self._invalidate(ManagedLeaseIssue.POST_TERMINAL)
        elif record.kind is FileRecordKind.FAILED and self.result is None and not self.failed:
            if record.body:
                self._invalidate(ManagedLeaseIssue.CONTROL)
            else:
                self.failed = True
        elif record.kind is FileRecordKind.RESULT and self.result is None and not self.failed:
            try:
                result = decode_result(record.body)
                if self.request.lease is None:
                    if not isinstance(result, ClockObservation):
                        raise LeaseError("clock result required")
                elif (
                    not isinstance(result, LeasePublication)
                    or result.accepted_expiry_ns != self.request.lease.expires_ns
                ):
                    raise LeaseError("exact publication expiry required")
                self.result = result
            except LeaseError:
                self._invalidate(ManagedLeaseIssue.CONTROL)
        elif record.kind is FileRecordKind.FINISHED:
            if record.body or (self.result is None) == (not self.failed):
                self._invalidate(ManagedLeaseIssue.ORDER)
            else:
                self.terminal = True
        else:
            self._invalidate(ManagedLeaseIssue.ORDER)

    def finish(
        self,
        wire_error: FileWireError | None,
        *,
        complete: bool,
        stderr_noise: bool,
        dispatch: Dispatch,
        completion: ExitStatus | None,
        carrier_failure: Failure | None,
        settled: bool,
        expired: bool,
    ) -> ManagedLeaseObservation:
        issue: ManagedLeaseIssue | FileWireError | None = self.issue or wire_error
        if issue is None and stderr_noise:
            issue = ManagedLeaseIssue.STDERR
        if issue is None and not complete:
            issue = ManagedLeaseIssue.CARRIER
        if issue is None and (carrier_failure is not None or completion is None or completion.code != 0):
            issue = ManagedLeaseIssue.COMPLETION
        if issue is None and not settled:
            issue = ManagedLeaseIssue.CUSTODY
        if issue is None and expired:
            issue = ManagedLeaseIssue.DEADLINE
        if issue is None and not self.terminal:
            issue = ManagedLeaseIssue.MISSING_TERMINAL
        if dispatch is not Dispatch.SENT:
            state = ManagedLeaseState.UNKNOWN if dispatch is Dispatch.UNKNOWN else ManagedLeaseState.INCOMPLETE
        elif issue is not None:
            state = (
                ManagedLeaseState.INCOMPLETE
                if issue
                in (
                    ManagedLeaseIssue.CARRIER,
                    ManagedLeaseIssue.CUSTODY,
                    ManagedLeaseIssue.DEADLINE,
                    ManagedLeaseIssue.MISSING_TERMINAL,
                    FileWireError.TRUNCATED,
                )
                else ManagedLeaseState.INVALID
            )
        elif self.failed:
            state = ManagedLeaseState.REFUSED
        else:
            state = ManagedLeaseState.OBSERVED if self.request.lease is None else ManagedLeaseState.PUBLISHED
        result = self.result if state in (ManagedLeaseState.OBSERVED, ManagedLeaseState.PUBLISHED) else None
        self.abort()
        return ManagedLeaseObservation(state, result, issue)


def _exchange(
    carrier: Carrier,
    *,
    expected_launch: bytes | None,
    lease: OperationLease | None,
    plan: IdentityPlan,
    guest: VMGuestIdentity,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    custody: LocalDeliveryCustody,
) -> ManagedLeaseCandidate:
    if type(runtime_selection) is not RuntimeSelection or runtime_selection.target_os is not RuntimeTargetOS.LINUX:
        raise ValidationError("Operation lease requires a Linux runtime")
    if _validate_plan(plan).euid != 0 or type(guest) is not VMGuestIdentity:
        raise ValidationError("Operation lease requires root and a full guest identity")
    if deadline.expires_at is None:
        raise ValidationError("Operation lease delivery requires a finite deadline")
    if type(custody) is not LocalDeliveryCustody:
        raise ValidationError("Operation lease delivery requires caller-held local custody")
    if not custody.settled:
        raise StateError("Previous operation lease delivery remains unsettled")
    request = LeaseRequest(secrets.token_hex(16), plan.expected, guest, expected_launch, lease)
    try:
        request_data = encode_request(request)
    except (LeaseError, ValueError, TypeError, AttributeError):
        raise ValidationError("Invalid operation lease request") from None
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
        invocation = PreparedInvocation(argv)
        carrier.validate(invocation, io=io)
        if deadline.expired:
            return ManagedLeaseCandidate(
                Dispatch.NOT_SENT,
                None,
                None,
                Failure.DEADLINE,
                RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None),
                None,
            )
        report = carrier.execute(invocation, io=io, deadline=deadline, custody=custody)
        prerequisite = runtime.observation
        observation = None
        if prerequisite.state is RuntimePrerequisiteState.READY:
            reader.finish()
            observation = collector.finish(
                reader.error,
                complete=(
                    report.stdout.retention is Retention.DELIVERED
                    and report.stderr.retention is Retention.DELIVERED
                    and report.stdout.complete
                    and report.stderr.complete
                ),
                stderr_noise=stderr.saw_data,
                dispatch=report.dispatch,
                completion=report.completion,
                carrier_failure=report.failure,
                settled=custody.settled,
                expired=deadline.expired,
            )
        return ManagedLeaseCandidate(
            report.dispatch, report.completion, report.local_status, report.failure, prerequisite, observation
        )
    finally:
        runtime.clear()
        reader.abort()
        collector.abort()
        stderr.clear()


def observe_operation_clock(
    carrier: Carrier,
    *,
    plan: IdentityPlan,
    guest: VMGuestIdentity,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    custody: LocalDeliveryCustody,
) -> ManagedLeaseCandidate:
    """Observe the exact guest clock under the caller's existing cycle deadline."""
    return _exchange(
        carrier,
        expected_launch=None,
        lease=None,
        plan=plan,
        guest=guest,
        runtime_selection=runtime_selection,
        deadline=deadline,
        custody=custody,
    )


def publish_operation_lease(
    carrier: Carrier,
    *,
    expected_launch: bytes,
    lease: OperationLease,
    plan: IdentityPlan,
    guest: VMGuestIdentity,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    custody: LocalDeliveryCustody,
) -> ManagedLeaseCandidate:
    """Publish the supplied sampled expiry without refreshing its clock or budget."""
    if type(expected_launch) is not bytes or type(lease) is not OperationLease:
        raise ValidationError("Operation lease publication requires an exact launch and lease")
    return _exchange(
        carrier,
        expected_launch=expected_launch,
        lease=lease,
        plan=plan,
        guest=guest,
        runtime_selection=runtime_selection,
        deadline=deadline,
        custody=custody,
    )
