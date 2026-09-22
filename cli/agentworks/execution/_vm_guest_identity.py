"""Private fixed-helper observation of VM guest identity."""

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
    build_runtime_helper_argv,
)
from agentworks.execution._vm_guest_identity_bundle import FIXED_SOURCE
from agentworks.execution._vm_guest_identity_protocol import (
    MAX_VM_GUEST_IDENTITY_MESSAGE_BYTES,
    VMGuestIdentity,
    VMGuestIdentityFailure,
    VMGuestIdentityResponseError,
    decode_vm_guest_identity_response,
)
from agentworks.execution.carrier import (
    CarrierIO,
    Dispatch,
    EndOfInput,
    Failure,
    PreparedInvocation,
    Retention,
    SinkOutput,
)

if TYPE_CHECKING:
    from agentworks.execution.carrier import Carrier, CarrierReport, Deadline, ExitStatus


class VMGuestIdentityObservationState(StrEnum):
    RESOLVED = "resolved"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"


class VMGuestIdentityObservationError(StrEnum):
    STREAMS = "streams"
    STDERR = "stderr"
    OVERSIZED = "oversized"
    MISSING = "missing"
    RESPONSE = "response"


@dataclass(frozen=True, slots=True)
class VMGuestIdentityObservation:
    state: VMGuestIdentityObservationState
    identity: VMGuestIdentity | None = None
    failure: VMGuestIdentityFailure | None = None
    error: VMGuestIdentityObservationError | None = None


@dataclass(frozen=True, slots=True)
class VMGuestIdentityObservationResult:
    """Carrier facts and the separate typed guest identity observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: VMGuestIdentityObservation | None


class _BoundedResponseSink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.oversized = False

    def try_write(self, data: memoryview) -> int:
        if data and not self.oversized:
            if len(data) > MAX_VM_GUEST_IDENTITY_MESSAGE_BYTES - len(self.data):
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
    response: _BoundedResponseSink,
    stderr: _DiagnosticSink,
    report: CarrierReport,
    nonce: str,
) -> VMGuestIdentityObservation:
    streams_complete = (
        report.stdout.retention is Retention.DELIVERED
        and report.stderr.retention is Retention.DELIVERED
        and report.stdout.complete
        and report.stderr.complete
    )
    if not streams_complete:
        return VMGuestIdentityObservation(
            VMGuestIdentityObservationState.INCOMPLETE,
            error=VMGuestIdentityObservationError.STREAMS,
        )
    if response.oversized:
        return VMGuestIdentityObservation(
            VMGuestIdentityObservationState.INVALID,
            error=VMGuestIdentityObservationError.OVERSIZED,
        )
    if stderr.saw_data:
        return VMGuestIdentityObservation(
            VMGuestIdentityObservationState.INVALID,
            error=VMGuestIdentityObservationError.STDERR,
        )
    if not response.data:
        return VMGuestIdentityObservation(
            VMGuestIdentityObservationState.INCOMPLETE,
            error=VMGuestIdentityObservationError.MISSING,
        )
    try:
        decoded = decode_vm_guest_identity_response(bytes(response.data), nonce)
    except VMGuestIdentityResponseError:
        return VMGuestIdentityObservation(
            VMGuestIdentityObservationState.INVALID,
            error=VMGuestIdentityObservationError.RESPONSE,
        )
    if decoded.identity is not None:
        return VMGuestIdentityObservation(VMGuestIdentityObservationState.RESOLVED, identity=decoded.identity)
    return VMGuestIdentityObservation(VMGuestIdentityObservationState.REFUSED, failure=decoded.failure)


def observe_vm_guest_identity(
    carrier: Carrier,
    *,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
) -> VMGuestIdentityObservationResult:
    """Make one no-replay fixed-helper attempt with no inherited stdin."""
    nonce = secrets.token_hex(16)
    if deadline.expired:
        return VMGuestIdentityObservationResult(
            Dispatch.NOT_SENT,
            None,
            None,
            Failure.DEADLINE,
            RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None),
            None,
        )
    argv, candidates, system_shim = build_runtime_helper_argv(
        selection=runtime_selection,
        fixed_source=FIXED_SOURCE,
        nonce=nonce,
    )
    invocation = PreparedInvocation(argv)
    response = _BoundedResponseSink()
    runtime = RuntimePrefixSink(nonce, candidates, response, system_shim)
    stderr = _DiagnosticSink()
    io = CarrierIO(input=EndOfInput(), output=SinkOutput(runtime, stderr, require_live=False))
    try:
        report = carrier.execute(invocation, io=io, deadline=deadline)
        prerequisite = runtime.observation
        observation = (
            _observation(response, stderr, report, nonce)
            if prerequisite.state is RuntimePrerequisiteState.READY
            else None
        )
        return VMGuestIdentityObservationResult(
            report.dispatch,
            report.completion,
            report.local_status,
            report.failure,
            prerequisite,
            observation,
        )
    finally:
        runtime.clear()
        response.clear()
        stderr.clear()
