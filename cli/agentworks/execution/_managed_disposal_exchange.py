"""Carrier-neutral private exchange for exact managed-run disposal."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from ._file_wire import FileRecord, FileRecordKind, FileRecordReader, FileWireError
from ._helper_launcher import IdentityPlan
from ._managed_disposal_bundle import FIXED_BUNDLE
from ._managed_disposal_protocol import DisposalError, DisposalRequest, DisposalResult, decode_result, encode_request
from ._managed_observation_protocol import ManagedObservationError, checked_launch
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


class DisposalState(StrEnum):
    DISPOSED = "disposed"
    NOT_READY = "not_ready"
    UNKNOWN = "unknown"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class DisposalObservation:
    state: DisposalState
    issue: str | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class DisposalCandidate:
    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: DisposalObservation | None


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
        self.result: DisposalResult | None = None
        self.receipt: bytes | None = None
        self.failed = False
        self.finished = False
        self.invalid = False

    def accept(self, record: FileRecord) -> None:
        if self.invalid or self.finished:
            self.invalid = True
        elif record.kind is FileRecordKind.FAILED and record.body == b"" and self.result is None and not self.failed:
            self.failed = True
        elif record.kind is FileRecordKind.RESULT and self.result is None and not self.failed:
            try:
                self.result = decode_result(record.body)
            except DisposalError:
                self.invalid = True
        elif record.kind is FileRecordKind.DATA and self.result is DisposalResult.DISPOSED and self.receipt is None:
            self.receipt = record.body
        elif record.kind is FileRecordKind.FINISHED and record.body == b"":
            self.finished = True
        else:
            self.invalid = True

    def finish(
        self,
        wire_error: FileWireError | None,
        *,
        complete: bool,
        stderr_noise: bool,
        dispatch: Dispatch,
        completion: ExitStatus | None,
        carrier_failure: Failure | None,
    ) -> DisposalObservation:
        if dispatch is not Dispatch.SENT:
            return DisposalObservation(
                DisposalState.UNKNOWN if dispatch is Dispatch.UNKNOWN else DisposalState.INCOMPLETE
            )
        if wire_error or not complete or not self.finished:
            return DisposalObservation(DisposalState.INCOMPLETE, wire_error or "transcript")
        if stderr_noise or carrier_failure is not None or completion is None or completion.code != 0:
            return DisposalObservation(DisposalState.UNKNOWN, "carrier")
        if self.invalid or self.failed:
            return DisposalObservation(DisposalState.UNKNOWN, "helper")
        if self.result is DisposalResult.DISPOSED and self.receipt == self.expected_launch:
            return DisposalObservation(DisposalState.DISPOSED)
        if self.result is DisposalResult.NOT_READY and self.receipt is None:
            return DisposalObservation(DisposalState.NOT_READY)
        return DisposalObservation(DisposalState.INCOMPLETE, "receipt")


def dispose_managed_run(
    carrier: Carrier,
    *,
    expected_launch: bytes,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> DisposalCandidate:
    """Dispatch only a fixed Linux root helper and preserve ambiguous effects."""
    if (
        type(deadline) is not Deadline
        or type(plan) is not IdentityPlan
        or type(runtime_selection) is not RuntimeSelection
        or runtime_selection.target_os is not RuntimeTargetOS.LINUX
        or plan.expected.euid != 0
    ):
        raise ValidationError("Managed disposal requires a Linux root helper and deadline")
    try:
        checked_launch(expected_launch)
        request = DisposalRequest(secrets.token_hex(16), expected_launch, plan.expected)
        request_data = encode_request(request)
        argv, candidates, shim = build_runtime_identity_helper_argv(
            plan, selection=runtime_selection, fixed_source=FIXED_BUNDLE.bootstrap, nonce=request.nonce
        )
    except (DisposalError, ManagedObservationError, ValueError, TypeError):
        raise ValidationError("Invalid managed disposal request") from None
    collector = _Collector(expected_launch)
    reader = FileRecordReader(request.nonce, collector.accept)
    runtime = RuntimePrefixSink(request.nonce, candidates, reader, shim)
    stderr = _DiagnosticSink()
    invocation = PreparedInvocation(argv)
    io = CarrierIO(
        input=FiniteInput(FIXED_BUNDLE.prefix + request_data, sensitive=True),
        output=SinkOutput(runtime, stderr, require_live=False),
        sensitive=True,
    )
    try:
        carrier.validate(invocation, io=io)
        if deadline.expired:
            return DisposalCandidate(
                Dispatch.NOT_SENT,
                None,
                None,
                Failure.DEADLINE,
                RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None),
                None,
            )
        report = carrier.execute(invocation, io=io, deadline=deadline)
        prerequisite = runtime.observation
        observation = None
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
        return DisposalCandidate(
            report.dispatch, report.completion, report.local_status, report.failure, prerequisite, observation
        )
    finally:
        runtime.clear()
        reader.abort()
        stderr.clear()
