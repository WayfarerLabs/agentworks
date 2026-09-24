"""One carrier-neutral attempt to publish and observe exact managed stop intent."""

from __future__ import annotations

import math
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from ._file_wire import FileRecord, FileRecordKind, FileRecordReader, FileWireError
from ._helper_launcher import IdentityPlan
from ._managed_observation_protocol import ManagedObservationError, checked_fact
from ._managed_stop_bundle import FIXED_BUNDLE
from ._managed_stop_protocol import (
    MAX_OBSERVATION_MS,
    ManagedStopError,
    ManagedStopRequest,
    ManagedStopResult,
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
    from ._managed_job_store import FactName
    from .carrier import Carrier, ExitStatus


class ManagedStopState(StrEnum):
    ACCEPTED = "accepted"
    TERMINATED = "terminated"
    INVALID = "invalid"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class ManagedStopIssue(StrEnum):
    ORDER = "order"
    CONTROL = "control"
    CONTENT = "content"
    POST_TERMINAL = "post_terminal"
    STDERR = "stderr"
    CARRIER = "carrier"
    COMPLETION = "completion"
    MISSING_TERMINAL = "missing_terminal"


@dataclass(frozen=True, slots=True)
class ManagedStopObservation:
    state: ManagedStopState
    facts: tuple[tuple[FactName, bytes], ...] = field(default=(), repr=False)
    issue: ManagedStopIssue | FileWireError | None = None


@dataclass(frozen=True, slots=True)
class ManagedStopCandidate:
    dispatch: Dispatch
    carrier_completion: ExitStatus | None
    carrier_local_status: int | None
    carrier_failure: Failure | None
    runtime_prerequisite: RuntimePrerequisiteObservation
    observation: ManagedStopObservation | None


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
        self.control: ManagedStopResult | None = None
        self.facts: list[tuple[FactName, bytes]] = []
        self.failed = False
        self.terminal = False
        self.issue: ManagedStopIssue | None = None

    def abort(self) -> None:
        self.control = None
        self.facts.clear()

    def _invalidate(self, issue: ManagedStopIssue) -> None:
        if self.issue is None:
            self.issue = issue
        self.abort()

    def accept(self, record: FileRecord) -> None:
        if self.issue is not None:
            return
        if self.terminal:
            self._invalidate(ManagedStopIssue.POST_TERMINAL)
        elif record.kind is FileRecordKind.FAILED and self.control is None and not self.failed:
            if record.body:
                self._invalidate(ManagedStopIssue.CONTROL)
            else:
                self.failed = True
        elif record.kind is FileRecordKind.RESULT and self.control is None and not self.failed:
            try:
                self.control = decode_result(record.body)
            except ManagedStopError:
                self._invalidate(ManagedStopIssue.CONTROL)
        elif (
            record.kind is FileRecordKind.DATA
            and self.control is not None
            and len(self.facts) < len(self.control.facts)
        ):
            name = self.control.facts[len(self.facts)]
            try:
                checked_fact(name, record.body, self.expected_launch)
            except ManagedObservationError:
                self._invalidate(ManagedStopIssue.CONTENT)
            else:
                self.facts.append((name, record.body))
        elif record.kind is FileRecordKind.FINISHED:
            if record.body or (self.control is None) == (not self.failed):
                self._invalidate(ManagedStopIssue.ORDER)
            else:
                self.terminal = True
        else:
            self._invalidate(ManagedStopIssue.ORDER)

    def finish(
        self,
        wire_error: FileWireError | None,
        *,
        complete: bool,
        stderr_noise: bool,
        dispatch: Dispatch,
        completion: ExitStatus | None,
        carrier_failure: Failure | None,
    ) -> ManagedStopObservation:
        issue: ManagedStopIssue | FileWireError | None = self.issue or wire_error
        if issue is None and stderr_noise:
            issue = ManagedStopIssue.STDERR
        if issue is None and not complete:
            issue = ManagedStopIssue.CARRIER
        if issue is None and (carrier_failure is not None or completion is None or completion.code != 0):
            issue = ManagedStopIssue.COMPLETION
        if issue is None and not self.terminal:
            issue = ManagedStopIssue.MISSING_TERMINAL
        if dispatch is not Dispatch.SENT:
            self.abort()
            return ManagedStopObservation(
                ManagedStopState.UNKNOWN if dispatch is Dispatch.UNKNOWN else ManagedStopState.INCOMPLETE,
                issue=issue,
            )
        if issue is not None:
            self.abort()
            return ManagedStopObservation(
                ManagedStopState.INCOMPLETE
                if issue in (ManagedStopIssue.CARRIER, ManagedStopIssue.MISSING_TERMINAL, FileWireError.TRUNCATED)
                else ManagedStopState.INVALID,
                issue=issue,
            )
        if self.failed:
            self.abort()
            return ManagedStopObservation(ManagedStopState.UNKNOWN)
        assert self.control is not None
        if len(self.facts) != len(self.control.facts):
            self.abort()
            return ManagedStopObservation(ManagedStopState.INCOMPLETE, issue=ManagedStopIssue.CONTENT)
        facts = tuple(self.facts)
        state = ManagedStopState.TERMINATED if len(facts) == 2 else ManagedStopState.ACCEPTED
        self.abort()
        return ManagedStopObservation(state, facts)


def stop_managed_run(
    carrier: Carrier,
    *,
    expected_launch: bytes,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
) -> ManagedStopCandidate:
    """Publish an idempotent stop request; only exact empty evidence proves termination."""
    if (
        type(deadline) is not Deadline
        or type(plan) is not IdentityPlan
        or type(runtime_selection) is not RuntimeSelection
        or runtime_selection.target_os is not RuntimeTargetOS.LINUX
        or plan.expected.euid != 0
    ):
        raise ValidationError("Managed stop requires a Linux root helper and deadline")
    try:
        remaining = deadline.remaining()
        budget = (
            MAX_OBSERVATION_MS if remaining is None else min(MAX_OBSERVATION_MS, max(1, math.ceil(remaining * 1000)))
        )
        request = ManagedStopRequest(secrets.token_hex(16), expected_launch, plan.expected, budget)
        request_data = encode_request(request)
        argv, candidates, shim = build_runtime_identity_helper_argv(
            plan, selection=runtime_selection, fixed_source=FIXED_BUNDLE.bootstrap, nonce=request.nonce
        )
    except (ManagedStopError, ManagedObservationError, ValueError, TypeError):
        raise ValidationError("Invalid managed stop request") from None
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
            return ManagedStopCandidate(
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
        return ManagedStopCandidate(
            report.dispatch, report.completion, report.local_status, report.failure, prerequisite, observation
        )
    finally:
        runtime.clear()
        reader.abort()
        collector.abort()
        stderr.clear()
