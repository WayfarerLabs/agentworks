"""One-attempt host exchange for private guest file-gate control."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._file_effect_gate_bundle import FIXED_BUNDLE
from agentworks.execution._file_effect_gate_protocol import (
    GateControlFailure,
    GateControlOperation,
    GateControlProtocolError,
    GateControlRequest,
    decode_gate_control_request,
    encode_gate_control_request,
    parse_gate_control_failure,
    parse_gate_control_finished,
    parse_gate_control_result,
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
    from agentworks.execution._file_effect_gate import FileEffectGateBinding
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.execution.carrier import Carrier, Deadline, ExitStatus


class GateControlObservationState(StrEnum):
    RESOLVED = "resolved"
    REFUSED = "refused"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"
    UNCERTAIN = "uncertain"


class GateControlObservationError(StrEnum):
    CONTROL = "control"
    ORDER = "order"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    MISSING_TERMINAL = "missing_terminal"
    COMPLETION = "completion"


@dataclass(frozen=True, slots=True)
class GateControlObservation:
    state: GateControlObservationState
    binding: FileEffectGateBinding | None = field(default=None, repr=False)
    failure: GateControlFailure | None = None
    error: GateControlObservationError | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class GateControlCandidateResult:
    """Keep carrier facts separate from one reduced helper observation."""

    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: GateControlObservation | None


class GateControlMutationUncertain(Exception):
    """A setup or generation advance may have reached its guest effect."""


class _DiagnosticSink:
    def __init__(self) -> None:
        self.saw_data = False

    def try_write(self, data: memoryview) -> int:
        if data:
            self.saw_data = True
        return len(data)


class _Collector:
    def __init__(self, request: GateControlRequest) -> None:
        self.request = request
        self.binding: FileEffectGateBinding | None = None
        self.failure: GateControlFailure | None = None
        self.terminal = False
        self.error: GateControlObservationError | None = None

    def accept(self, record: FileRecord) -> None:
        if self.error is not None:
            return
        if self.terminal:
            self._fail(GateControlObservationError.POST_TERMINAL)
            return
        try:
            if record.kind is FileRecordKind.RESULT:
                if self.binding is not None or self.failure is not None:
                    self._fail(GateControlObservationError.ORDER)
                else:
                    binding = parse_gate_control_result(record.body, self.request)
                    if not self._matches(binding):
                        self._fail(GateControlObservationError.CONTROL)
                    else:
                        self.binding = binding
            elif record.kind is FileRecordKind.FAILED:
                if self.binding is not None or self.failure is not None:
                    self._fail(GateControlObservationError.ORDER)
                else:
                    self.failure = parse_gate_control_failure(record.body)
            elif record.kind is FileRecordKind.FINISHED:
                parse_gate_control_finished(record.body)
                if (self.binding is None) == (self.failure is None):
                    self._fail(GateControlObservationError.ORDER)
                else:
                    self.terminal = True
            else:
                self._fail(GateControlObservationError.ORDER)
        except GateControlProtocolError:
            self._fail(GateControlObservationError.CONTROL)

    def _matches(self, binding: FileEffectGateBinding) -> bool:
        request = self.request
        if (
            binding.path != request.path
            or binding.guest != request.guest
            or binding.euid != request.euid
            or binding.scope_name != request.scope_name
        ):
            return False
        if request.operation is not GateControlOperation.ADVANCE:
            return True
        old = request.binding
        assert old is not None
        return (
            binding.instance == old.instance
            and binding.generation == old.proposed_generation
            and (binding.device, binding.inode) == (old.device, old.inode)
        )

    def _fail(self, error: GateControlObservationError) -> None:
        self.error = error
        self.abort()

    def abort(self) -> None:
        self.binding = None
        self.failure = None
        self.terminal = False

    def finish(
        self,
        wire_error: FileWireError | None,
        *,
        streams_complete: bool,
        stderr_noise: bool,
        completion: ExitStatus | None,
        dispatch: Dispatch,
    ) -> GateControlObservation:
        error: GateControlObservationError | FileWireError | None = self.error or wire_error
        if error is None and stderr_noise:
            error = GateControlObservationError.STDERR
        if error is None and not streams_complete:
            error = GateControlObservationError.CARRIER
        if error is None and not self.terminal:
            error = GateControlObservationError.MISSING_TERMINAL
        if error is None and (completion is None or completion.code != 0):
            error = GateControlObservationError.COMPLETION
        mutation = self.request.operation is not GateControlOperation.INSPECT and dispatch is not Dispatch.NOT_SENT
        if error is not None:
            self.abort()
            if mutation:
                state = GateControlObservationState.UNCERTAIN
            elif error in {
                GateControlObservationError.CARRIER,
                GateControlObservationError.MISSING_TERMINAL,
                GateControlObservationError.COMPLETION,
                FileWireError.TRUNCATED,
            }:
                state = GateControlObservationState.INCOMPLETE
            else:
                state = GateControlObservationState.INVALID
            return GateControlObservation(state, error=error)
        if self.failure is not None:
            failure = self.failure
            self.abort()
            state = (
                GateControlObservationState.UNCERTAIN
                if mutation and failure is GateControlFailure.GATE
                else GateControlObservationState.REFUSED
            )
            return GateControlObservation(state, failure=failure)
        assert self.binding is not None
        binding = self.binding
        self.abort()
        return GateControlObservation(GateControlObservationState.RESOLVED, binding=binding)


def exchange_file_effect_gate(
    carrier: Carrier,
    *,
    operation: GateControlOperation,
    path: str,
    guest: VMGuestIdentity,
    scope_name: str,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    binding: FileEffectGateBinding | None = None,
) -> GateControlCandidateResult:
    """Perform one non-replayed setup, inspection or CAS attempt."""
    nonce = secrets.token_hex(16)
    fixed_argv, candidates, system_shim = build_runtime_identity_helper_argv(
        plan, selection=runtime_selection, fixed_source=FIXED_BUNDLE.bootstrap, nonce=nonce
    )
    try:
        manifest = encode_gate_control_request(
            GateControlRequest(
                nonce,
                operation,
                path,
                guest,
                plan.expected.euid,
                scope_name,
                plan.expected,
                deadline.remaining(),
                binding,
            )
        )
    except GateControlProtocolError:
        raise ValidationError("File-gate control request is invalid or exceeds its 32768-byte bound") from None
    request = decode_gate_control_request(manifest)
    collector = _Collector(request)
    reader = FileRecordReader(nonce, collector.accept)
    runtime = RuntimePrefixSink(nonce, candidates, reader, system_shim)
    stderr = _DiagnosticSink()
    io = CarrierIO(
        input=FiniteInput(FIXED_BUNDLE.prefix + manifest, sensitive=True),
        output=SinkOutput(runtime, stderr, require_live=False),
        sensitive=True,
    )
    try:
        report = carrier.execute(PreparedInvocation(fixed_argv), io=io, deadline=deadline)
        prerequisite = runtime.observation
        if prerequisite.state is RuntimePrerequisiteState.READY:
            reader.finish()
            delivered = (
                report.stdout.retention is Retention.DELIVERED and report.stderr.retention is Retention.DELIVERED
            )
            observation = collector.finish(
                reader.error,
                streams_complete=delivered and report.stdout.complete and report.stderr.complete,
                stderr_noise=stderr.saw_data,
                completion=report.completion,
                dispatch=report.dispatch,
            )
        else:
            reader.abort()
            collector.abort()
            observation = None
        return GateControlCandidateResult(
            report.dispatch, report.completion, report.local_status, report.failure, prerequisite, observation
        )
    except BaseException as control:
        reader.abort()
        collector.abort()
        if operation is not GateControlOperation.INSPECT:
            raise control from GateControlMutationUncertain()
        raise
    finally:
        runtime.clear()
