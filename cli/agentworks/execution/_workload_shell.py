"""Private one-attempt workload shell observation under a bound identity plan."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.execution._runtime_prerequisite import (
    RuntimePrefixSink,
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    build_runtime_identity_helper_argv,
)
from agentworks.execution._workload_shell_bundle import FIXED_SOURCE
from agentworks.execution._workload_shell_protocol import (
    MAX_WORKLOAD_SHELL_RESPONSE_BYTES,
    WorkloadShellFailure,
    WorkloadShellRequest,
    WorkloadShellWireError,
    decode_workload_shell_response,
    encode_workload_shell_request,
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
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution.carrier import Carrier, CarrierReport, Deadline, ExitStatus


class WorkloadShellObservationState(StrEnum):
    RESOLVED = "resolved"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"


class WorkloadShellObservationError(StrEnum):
    STREAMS = "streams"
    STDERR = "stderr"
    OVERSIZED = "oversized"
    MISSING = "missing"
    RESPONSE = "response"


@dataclass(frozen=True, slots=True)
class WorkloadShellObservation:
    state: WorkloadShellObservationState
    shell: str | None = None
    failure: WorkloadShellFailure | None = None
    error: WorkloadShellObservationError | None = None


@dataclass(frozen=True, slots=True)
class WorkloadShellObservationResult:
    """Raw carrier facts, runtime prerequisite, and separate helper observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: WorkloadShellObservation | None


class _BoundedResponseSink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.oversized = False

    def try_write(self, data: memoryview) -> int:
        if data and not self.oversized:
            if len(data) > MAX_WORKLOAD_SHELL_RESPONSE_BYTES - len(self.data):
                self.data.clear()
                self.oversized = True
            else:
                self.data.extend(data)
        return len(data)

    def clear(self) -> None:
        self.data.clear()
        self.oversized = False


class _DiagnosticSink:
    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        if data:
            self.saw_data = True
        return len(data)

    def clear(self) -> None:
        self.saw_data = False


def _observation(
    response: _BoundedResponseSink, stderr: _DiagnosticSink, report: CarrierReport, nonce: str
) -> WorkloadShellObservation:
    if (
        report.stdout.retention is not Retention.DELIVERED
        or report.stderr.retention is not Retention.DELIVERED
        or not report.stdout.complete
        or not report.stderr.complete
    ):
        return WorkloadShellObservation(
            WorkloadShellObservationState.INCOMPLETE, error=WorkloadShellObservationError.STREAMS
        )
    if response.oversized:
        return WorkloadShellObservation(
            WorkloadShellObservationState.INVALID, error=WorkloadShellObservationError.OVERSIZED
        )
    if stderr.saw_data:
        return WorkloadShellObservation(
            WorkloadShellObservationState.INVALID, error=WorkloadShellObservationError.STDERR
        )
    if not response.data:
        return WorkloadShellObservation(
            WorkloadShellObservationState.INCOMPLETE, error=WorkloadShellObservationError.MISSING
        )
    try:
        decoded = decode_workload_shell_response(bytes(response.data), nonce)
    except WorkloadShellWireError:
        return WorkloadShellObservation(
            WorkloadShellObservationState.INVALID, error=WorkloadShellObservationError.RESPONSE
        )
    if decoded.shell is not None:
        return WorkloadShellObservation(WorkloadShellObservationState.RESOLVED, shell=decoded.shell)
    return WorkloadShellObservation(WorkloadShellObservationState.REFUSED, failure=decoded.failure)


def observe_workload_shell(
    carrier: Carrier,
    *,
    plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
) -> WorkloadShellObservationResult:
    """Make one bounded read-only attempt; never infer a shell from carrier status."""
    nonce = secrets.token_hex(16)
    request = encode_workload_shell_request(WorkloadShellRequest(nonce, plan.expected))
    if deadline.expired:
        return WorkloadShellObservationResult(
            Dispatch.NOT_SENT,
            None,
            None,
            Failure.DEADLINE,
            RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None),
            None,
        )
    argv, candidates, system_shim = build_runtime_identity_helper_argv(
        plan, selection=runtime_selection, fixed_source=FIXED_SOURCE, nonce=nonce
    )
    response = _BoundedResponseSink()
    runtime = RuntimePrefixSink(nonce, candidates, response, system_shim)
    stderr = _DiagnosticSink()
    io = CarrierIO(
        input=FiniteInput(request, sensitive=True),
        output=SinkOutput(runtime, stderr, require_live=False),
        sensitive=True,
    )
    try:
        report = carrier.execute(PreparedInvocation(argv), io=io, deadline=deadline)
        prerequisite = runtime.observation
        observation = (
            _observation(response, stderr, report, nonce)
            if prerequisite.state is RuntimePrerequisiteState.READY
            else None
        )
        return WorkloadShellObservationResult(
            report.dispatch, report.completion, report.local_status, report.failure, prerequisite, observation
        )
    finally:
        runtime.clear()
        response.clear()
        stderr.clear()
