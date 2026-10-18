"""One private, bounded reduction of an exactly bound managed VM run."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from ._managed_bound_run import ManagedDeadlineExpired, preflight_bound_read
from ._managed_job_protocol import (
    BoundaryEmptyFact,
    ManagedJobFactError,
    StreamDisposition,
    StreamEndFact,
    WorkloadWaitFact,
    decode_managed_job_fact,
)
from ._managed_job_store import FactName, Stream
from ._managed_observation_exchange import ManagedObservationState
from ._managed_observation_protocol import MAX_OUTPUT_PAGE_BYTES
from ._managed_observe_access import (
    ManagedObserveControlFact,
    ManagedObserveOutcome,
    observe_bound_managed_run,
    read_bound_managed_output,
)
from ._managed_runs import (
    ManagedLaunchState,
    ManagedOutputMode,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunRecord,
    ManagedRunRepository,
)
from .carrier import Deadline, Dispatch, Failure, Retention
from .models import JobRef
from .result import ApplicationState, ExecutionFailure, ExecutionOutput, ExecutionResult, ExitCode, Signal

if TYPE_CHECKING:
    from agentworks.operations import OperationOwner

    from ._execution_operation import ExecutionOperation
    from ._helper_launcher import IdentityPlan
    from ._managed_runs import ManagedRunOwner, ManagedTargetIdentity
    from ._runtime_prerequisite import RuntimeSelection
    from ._vm_guest_identity_protocol import VMGuestIdentity
    from .carrier import Carrier


@dataclass(frozen=True, slots=True, repr=False)
class ManagedResultOutcome:
    """Public result and private custody for the attempts actually made.

    A completed result says nothing about a prior start obligation. That
    obligation belongs to the launch reconciliation lifecycle.
    """

    result: ExecutionResult
    attempts: tuple[ManagedObserveOutcome, ...]
    awaiting_facts: bool = False

    @property
    def requires_owner_retention(self) -> bool:
        return any(attempt.requires_owner_retention for attempt in self.attempts)


class ManagedResultControlFact(Exception):
    """Custody from completed attempts when result reduction escapes."""

    def __init__(self, attempts: tuple[ManagedObserveOutcome, ...]) -> None:
        self.attempts = attempts
        super().__init__("managed result collection stopped with operation state")


def wait_bound_managed_result(
    repository: ManagedRunRepository,
    identity: ManagedRunIdentity,
    *,
    target: ManagedTargetIdentity,
    guest: VMGuestIdentity,
    root_plan: IdentityPlan,
    carrier: Carrier,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    owner: OperationOwner,
    expected_resource_owner: ManagedRunOwner | None = None,
    execution_operation: ExecutionOperation | None = None,
) -> ManagedResultOutcome:
    """Poll only a clean, settled pending run under one finite deadline.

    Each poll is a new read-only observation, never a launch retry. A failed
    or uncertain attempt returns immediately with its exact custody; earlier
    settled observations need no retained attempt ledger.
    """
    previous: ManagedResultOutcome | None = None
    while True:
        try:
            outcome = collect_bound_managed_result(
                repository,
                identity,
                target=target,
                guest=guest,
                root_plan=root_plan,
                carrier=carrier,
                runtime_selection=runtime_selection,
                deadline=deadline,
                owner=owner,
                expected_resource_owner=expected_resource_owner,
                execution_operation=execution_operation,
            )
        except ManagedDeadlineExpired as error:
            if previous is None or error.__cause__ is not None:
                raise
            return _expired(previous)
        if not outcome.awaiting_facts:
            return outcome
        previous = outcome
        remaining = deadline.remaining()
        if remaining is None:
            raise ValidationError("Managed wait requires a finite deadline")
        if remaining <= 0:
            return _expired(outcome)
        time.sleep(min(0.1, remaining))
        if deadline.expired:
            return _expired(outcome)


def _expired(outcome: ManagedResultOutcome) -> ManagedResultOutcome:
    result = replace(outcome.result, failure=ExecutionFailure.DEADLINE, deadline_exceeded=True)
    return ManagedResultOutcome(result, outcome.attempts)


def collect_bound_managed_result(
    repository: ManagedRunRepository,
    identity: ManagedRunIdentity,
    *,
    target: ManagedTargetIdentity,
    guest: VMGuestIdentity,
    root_plan: IdentityPlan,
    carrier: Carrier,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    owner: OperationOwner,
    expected_resource_owner: ManagedRunOwner | None = None,
    execution_operation: ExecutionOperation | None = None,
) -> ManagedResultOutcome:
    """Collect once under one finite deadline and caller-held owner.

    The bound access helpers validate the persisted run before borrowing. A
    retained owner ends this attempt; the caller owns all later recovery.
    """
    record, _ = preflight_bound_read(
        repository,
        identity,
        target=target,
        guest=guest,
        root_plan=root_plan,
        runtime_selection=runtime_selection,
        deadline=deadline,
        owner=owner,
        expected_resource_owner=expected_resource_owner,
        execution_operation=execution_operation,
    )
    if record.launch_state is not ManagedLaunchState.RECEIPT_CONFIRMED:
        raise ValidationError("Managed result requires a reconciled launch receipt")
    attempts: list[ManagedObserveOutcome] = []
    try:
        observed = observe_bound_managed_run(
            repository,
            identity,
            target=target,
            guest=guest,
            root_plan=root_plan,
            carrier=carrier,
            runtime_selection=runtime_selection,
            deadline=deadline,
            owner=owner,
            expected_resource_owner=expected_resource_owner,
            execution_operation=execution_operation,
        )
        attempts.append(observed)
        return _reduce(
            repository,
            identity,
            record,
            attempts,
            target=target,
            guest=guest,
            root_plan=root_plan,
            carrier=carrier,
            runtime_selection=runtime_selection,
            deadline=deadline,
            owner=owner,
            expected_resource_owner=expected_resource_owner,
            execution_operation=execution_operation,
        )
    except BaseException as control:
        if isinstance(control.__cause__, ManagedObserveControlFact):
            attempts.append(control.__cause__.outcome)
        if not attempts:
            raise
        custody = ManagedResultControlFact(tuple(attempts))
        custody.__cause__ = control.__cause__
        raise control from custody


def _reduce(
    repository: ManagedRunRepository,
    identity: ManagedRunIdentity,
    record: ManagedRunRecord,
    attempts: list[ManagedObserveOutcome],
    *,
    target: ManagedTargetIdentity,
    guest: VMGuestIdentity,
    root_plan: IdentityPlan,
    carrier: Carrier,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    owner: OperationOwner,
    expected_resource_owner: ManagedRunOwner | None = None,
    execution_operation: ExecutionOperation | None = None,
) -> ManagedResultOutcome:
    observed = attempts[0]
    candidate = observed.candidate
    observation = None if candidate is None else candidate.observation
    facts = (
        dict(observation.facts)
        if observation is not None and observation.state is ManagedObservationState.OBSERVED
        else {}
    )
    wait = _fact(facts.get(FactName.WAIT), WorkloadWaitFact)
    boundary = _fact(facts.get(FactName.BOUNDARY_EMPTY), BoundaryEmptyFact)
    status = None
    if isinstance(wait, WorkloadWaitFact):
        status = ExitCode(wait.exit_code) if wait.exit_code is not None else Signal(wait.signal)  # type: ignore[arg-type]
    state = ApplicationState.COMPLETED if status is not None else ApplicationState.UNKNOWN
    outputs: dict[Stream, ExecutionOutput] = {}
    retrieved: set[Stream] = set()
    output_failure: ExecutionFailure | None = None
    refused_read = False
    for stream in Stream:
        end_name = FactName.STDOUT_END if stream is Stream.STDOUT else FactName.STDERR_END
        observed_end = facts.get(end_name)
        if (
            any(
                attempt.requires_owner_retention or attempt.pending_remote_effects or attempt.coordination_uncertain
                for attempt in attempts
            )
            or deadline.expired
        ):
            break
        if observed_end is None:
            continue
        end = decode_managed_job_fact(observed_end)
        assert isinstance(end, StreamEndFact)
        if end.disposition is StreamDisposition.TRUNCATED_CAPTURE:
            output_failure = ExecutionFailure.OUTPUT_LIMIT
        prefix = bytearray()
        accepted_page = False
        while True:
            if deadline.expired:
                break
            try:
                read = read_bound_managed_output(
                    repository,
                    identity,
                    stream=stream,
                    offset=len(prefix),
                    count=MAX_OUTPUT_PAGE_BYTES,
                    target=target,
                    guest=guest,
                    root_plan=root_plan,
                    carrier=carrier,
                    runtime_selection=runtime_selection,
                    deadline=deadline,
                    owner=owner,
                    expected_resource_owner=expected_resource_owner,
                    execution_operation=execution_operation,
                )
            except ManagedDeadlineExpired as error:
                # Expiry before borrowing preserves verified prior pages.
                # Control carrying borrowed custody must still escape.
                if error.__cause__ is not None:
                    raise
                break
            attempts.append(read.attempt)
            read_facts = (
                read.attempt.candidate.observation.facts
                if read.attempt.candidate is not None and read.attempt.candidate.observation is not None
                else ()
            )
            if (
                not read.accepted
                or read.attempt.requires_owner_retention
                or read.attempt.pending_remote_effects
                or read.attempt.coordination_uncertain
                or len(read_facts) != 2
                or read_facts[1] != (end_name, observed_end)
            ):
                refused_read = True
                break
            accepted_page = True
            if read.disposition in (StreamDisposition.DISCARDED, StreamDisposition.SUPPRESSED):
                outputs[stream] = ExecutionOutput(
                    b"",
                    True,
                    Retention.DISCARDED if read.disposition is StreamDisposition.DISCARDED else Retention.SUPPRESSED,
                )
                retrieved.add(stream)
                break
            assert read.output is not None
            prefix.extend(read.output)
            if len(prefix) == end.retained_bytes:
                retrieved.add(stream)
                break
        if accepted_page and end.disposition in (
            StreamDisposition.COMPLETE_CAPTURE,
            StreamDisposition.TRUNCATED_CAPTURE,
        ):
            outputs[stream] = ExecutionOutput(
                bytes(prefix),
                stream in retrieved and end.disposition is StreamDisposition.COMPLETE_CAPTURE,
                Retention.CAPTURED,
            )
        if refused_read:
            break

    retention = (
        Retention.CAPTURED
        if record.output_policy.mode is ManagedOutputMode.CAPTURE
        else Retention.DISCARDED
        if record.output_policy.mode is ManagedOutputMode.DISCARD
        else Retention.SUPPRESSED
    )
    missing = ExecutionOutput(b"", False, retention)
    settled = not any(
        attempt.requires_owner_retention or attempt.pending_remote_effects or attempt.coordination_uncertain
        for attempt in attempts
    )
    deadline_exceeded = deadline.expired or any(
        attempt.candidate is not None and attempt.candidate.carrier_failure is Failure.DEADLINE for attempt in attempts
    )
    failure = (
        ExecutionFailure.DEADLINE
        if deadline_exceeded
        else ExecutionFailure.OBSERVATION
        if observation is None
        or observation.state is not ManagedObservationState.OBSERVED
        or not settled
        or boundary is None
        or status is None
        or refused_read
        else ExecutionFailure.OUTPUT
        if len(retrieved) != 2
        else output_failure
    )
    result = ExecutionResult(
        dispatch=Dispatch.UNKNOWN,
        application_state=state,
        status=status,
        stdout=outputs.get(Stream.STDOUT, missing),
        stderr=outputs.get(Stream.STDERR, missing),
        failure=failure,
        owned_cleanup_confirmed=boundary is not None and settled,
        deadline_exceeded=deadline_exceeded,
    )
    can_poll = (
        candidate is not None
        and candidate.carrier_failure is None
        and observation is not None
        and observation.state is ManagedObservationState.OBSERVED
        and settled
        and not deadline_exceeded
        and not refused_read
        and output_failure is None
    )
    awaiting_facts = (
        can_poll
        and (status is not None or boundary is None)
        and (status is None or boundary is None or len(retrieved) != 2)
    )
    if execution_operation is not None:
        if record.spec.lifetime is ManagedRunLifetime.OPERATION:
            closed = settled and execution_operation.retain_job_terminal_observation(JobRef(identity.run_id), candidate)
        else:
            closed = settled and observed.terminal_proved
        result = replace(result, owned_cleanup_confirmed=result.owned_cleanup_confirmed and closed)
        awaiting_facts = can_poll and not closed
    return ManagedResultOutcome(result, tuple(attempts), awaiting_facts)


def _fact(
    data: bytes | None, expected: type[WorkloadWaitFact] | type[BoundaryEmptyFact]
) -> WorkloadWaitFact | BoundaryEmptyFact | None:
    if data is None:
        return None
    try:
        fact = decode_managed_job_fact(data)
    except ManagedJobFactError:
        return None
    return fact if isinstance(fact, expected) else None
