"""One private, bounded reduction of an independent managed VM run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from ._managed_bound_run import preflight_bound_run
from ._managed_job_protocol import (
    BoundaryEmptyFact,
    ManagedJobFactError,
    StreamDisposition,
    WorkloadWaitFact,
    decode_managed_job_fact,
)
from ._managed_job_store import FactName, Stream
from ._managed_observation_exchange import ManagedObservationState
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
    ManagedRunRecord,
    ManagedRunRepository,
)
from .carrier import Deadline, Dispatch, Failure, Retention
from .result import ApplicationState, ExecutionFailure, ExecutionOutput, ExecutionResult, ExitCode, Signal

if TYPE_CHECKING:
    from agentworks.operations import OperationOwner

    from ._helper_launcher import IdentityPlan
    from ._managed_runs import ManagedTargetIdentity
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

    @property
    def requires_owner_retention(self) -> bool:
        return any(attempt.requires_owner_retention for attempt in self.attempts)


class ManagedResultControlFact(Exception):
    """Custody from completed attempts when result reduction escapes."""

    def __init__(self, attempts: tuple[ManagedObserveOutcome, ...]) -> None:
        self.attempts = attempts
        super().__init__("managed result collection stopped with operation state")


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
) -> ManagedResultOutcome:
    """Collect once under one finite deadline and caller-held owner.

    The bound access helpers validate the persisted run before borrowing. A
    retained owner ends this attempt; the caller owns all later recovery.
    """
    record, _ = preflight_bound_run(
        repository,
        identity,
        target=target,
        guest=guest,
        root_plan=root_plan,
        runtime_selection=runtime_selection,
        deadline=deadline,
        owner=owner,
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
        )
    except BaseException as control:
        if isinstance(control.__cause__, ManagedObserveControlFact):
            attempts.append(control.__cause__.outcome)
        if not attempts:
            raise
        raise control from ManagedResultControlFact(tuple(attempts))


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
    output_failure: ExecutionFailure | None = None
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
        try:
            read = read_bound_managed_output(
                repository,
                identity,
                stream=stream,
                target=target,
                guest=guest,
                root_plan=root_plan,
                carrier=carrier,
                runtime_selection=runtime_selection,
                deadline=deadline,
                owner=owner,
            )
        except ValidationError as error:
            # The bound reader refuses an expired deadline before borrowing.
            # Preserve the prior observation as a partial result in that case;
            # a failure carrying borrowed custody must still escape.
            if not deadline.expired or error.__cause__ is not None:
                raise
            break
        attempts.append(read.attempt)
        read_facts = (
            read.attempt.candidate.observation.facts
            if read.attempt.candidate is not None and read.attempt.candidate.observation is not None
            else ()
        )
        # A separately admitted read must match the exact end seen in the
        # observation snapshot before it can settle this result.
        if (
            read.accepted
            and observed_end is not None
            and len(read_facts) == 2
            and read_facts[1] == (end_name, observed_end)
        ):
            if read.disposition is StreamDisposition.COMPLETE_CAPTURE:
                outputs[stream] = ExecutionOutput(read.output or b"", True, Retention.CAPTURED)
            elif read.disposition is StreamDisposition.TRUNCATED_CAPTURE:
                outputs[stream] = ExecutionOutput(read.output or b"", False, Retention.CAPTURED)
                output_failure = ExecutionFailure.OUTPUT_LIMIT
            elif read.disposition is StreamDisposition.DISCARDED:
                outputs[stream] = ExecutionOutput(b"", True, Retention.DISCARDED)
            elif read.disposition is StreamDisposition.SUPPRESSED:
                outputs[stream] = ExecutionOutput(b"", True, Retention.SUPPRESSED)
        if (
            read.attempt.requires_owner_retention
            or read.attempt.pending_remote_effects
            or read.attempt.coordination_uncertain
        ):
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
        else ExecutionFailure.OUTPUT
        if len(outputs) != 2
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
    return ManagedResultOutcome(result, tuple(attempts))


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
