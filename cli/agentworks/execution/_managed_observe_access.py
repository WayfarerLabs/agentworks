"""Private exact-run observation under a caller-held VM operation owner."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError

from ._managed_bound_run import preflight_bound_read
from ._managed_job_protocol import (
    ManagedJobFactError,
    StreamDisposition,
    StreamEndFact,
    decode_managed_job_fact,
    encode_managed_job_fact,
)
from ._managed_job_store import FactName, Stream
from ._managed_observation_exchange import (
    ManagedObservationCandidate,
    ManagedObservationState,
)
from ._managed_runs import (
    ManagedLaunchObservation,
    ManagedLaunchState,
    ManagedOutputMode,
    ManagedRunIdentity,
    ManagedRunReceipt,
    ManagedRunRepository,
    ManagedTargetIdentity,
)
from ._managed_terminal import terminal_observation_proved
from .carrier import Dispatch

if TYPE_CHECKING:
    from agentworks.operations import OperationOwner

    from ._execution_operation import ExecutionOperation
    from ._helper_launcher import IdentityPlan
    from ._managed_runs import ManagedRunRecord
    from ._runtime_prerequisite import RuntimeSelection
    from ._vm_guest_identity_protocol import VMGuestIdentity
    from .carrier import Carrier, Deadline


@dataclass(frozen=True, slots=True, repr=False)
class ManagedObserveOutcome:
    """One-attempt evidence and explicit operation custody state."""

    candidate: ManagedObservationCandidate | None = field(default=None, repr=False)
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False
    terminal_proved: bool = False


class ManagedObserveControlFact(Exception):
    """Custody facts attached as the cause of escaping control flow."""

    def __init__(self, outcome: ManagedObserveOutcome) -> None:
        self.outcome = outcome
        super().__init__("managed observation stopped with retained operation state")


@dataclass(frozen=True, slots=True, repr=False)
class ManagedReadOutputOutcome:
    """One sanitized attempt and its policy-admitted selected stream, if any."""

    attempt: ManagedObserveOutcome
    disposition: StreamDisposition | None = None
    output: bytes | None = field(default=None, repr=False)
    retained_bytes: int | None = None

    @property
    def accepted(self) -> bool:
        return self.disposition is not None


def observe_and_reconcile_bound_managed_run(
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
    execution_operation: ExecutionOperation,
) -> ManagedObserveOutcome:
    """Observe one exact run and reconcile only a validated launch receipt."""
    record, candidate, outcome = _bound_exchange(
        repository,
        identity,
        target=target,
        guest=guest,
        root_plan=root_plan,
        carrier=carrier,
        runtime_selection=runtime_selection,
        deadline=deadline,
        owner=owner,
        execution_operation=execution_operation,
        stream=None,
    )
    try:
        observation = None if candidate is None else candidate.observation
        if observation is None or observation.state is not ManagedObservationState.OBSERVED:
            return outcome
        if not observation.facts or observation.facts[0][0] is not FactName.LAUNCH:
            return outcome
        try:
            receipt = decode_managed_job_fact(observation.facts[0][1])
        except ManagedJobFactError:
            return outcome
        if not isinstance(receipt, ManagedRunReceipt):
            return outcome
        repository.reconcile(
            record,
            ManagedLaunchObservation(Dispatch.UNKNOWN, receipt),
        )
    except BaseException as control:
        fact = ManagedObserveControlFact(outcome)
        fact.__cause__ = control.__cause__
        raise control from fact
    return outcome


def observe_bound_managed_run(
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
    execution_operation: ExecutionOperation,
) -> ManagedObserveOutcome:
    """Read one exact VM run under its bound dispatch custody.

    The caller supplies freshly prepared target and guest facts and retains
    responsibility for route freshness. This does not reconcile launch state,
    reduce output policy, or release the caller's operation owner. Operation
    runs require the concrete retained execution context; independent runs require
    that context's constructor-bound resource owner.
    """
    record, _, outcome = _bound_exchange(
        repository,
        identity,
        target=target,
        guest=guest,
        root_plan=root_plan,
        carrier=carrier,
        runtime_selection=runtime_selection,
        deadline=deadline,
        owner=owner,
        stream=None,
        execution_operation=execution_operation,
    )
    receipt = ManagedRunReceipt(record.identity, record.identity.unit_name, record.spec)
    return replace(
        outcome,
        terminal_proved=record.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
        and not outcome.requires_owner_retention
        and terminal_observation_proved(outcome.candidate, encode_managed_job_fact(receipt)),
    )


def read_bound_managed_output(
    repository: ManagedRunRepository,
    identity: ManagedRunIdentity,
    *,
    stream: Stream,
    offset: int,
    count: int,
    target: ManagedTargetIdentity,
    guest: VMGuestIdentity,
    root_plan: IdentityPlan,
    carrier: Carrier,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    owner: OperationOwner,
    execution_operation: ExecutionOperation,
) -> ManagedReadOutputOutcome:
    """Read one exact closed stream and admit it under persisted output policy."""
    if type(stream) is not Stream:
        raise ValidationError("Managed output requires one selected stream")
    record, raw_candidate, attempt = _bound_exchange(
        repository,
        identity,
        target=target,
        guest=guest,
        root_plan=root_plan,
        carrier=carrier,
        runtime_selection=runtime_selection,
        deadline=deadline,
        owner=owner,
        stream=stream,
        offset=offset,
        count=count,
        execution_operation=execution_operation,
    )
    try:
        disposition, output, retained_bytes = _admit_output(record, raw_candidate)
    except BaseException as control:
        raise control from ManagedObserveControlFact(attempt)
    return ManagedReadOutputOutcome(attempt, disposition, output, retained_bytes)


def _bound_exchange(
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
    stream: Stream | None,
    execution_operation: ExecutionOperation,
    offset: int | None = None,
    count: int | None = None,
) -> tuple[ManagedRunRecord, ManagedObservationCandidate | None, ManagedObserveOutcome]:
    record, expected_launch = preflight_bound_read(
        repository,
        identity,
        target=target,
        guest=guest,
        root_plan=root_plan,
        runtime_selection=runtime_selection,
        deadline=deadline,
        owner=owner,
        execution_operation=execution_operation,
    )

    candidate, outcome = execution_operation.observe_managed(
        carrier,
        expected_launch=expected_launch,
        root_plan=root_plan,
        runtime_selection=runtime_selection,
        deadline=deadline,
        guest=guest,
        stream=stream,
        offset=offset,
        count=count,
    )
    return record, candidate, outcome


def _without_output(candidate: ManagedObservationCandidate | None) -> ManagedObservationCandidate | None:
    """Keep read evidence while stripping bytes before it leaves this module."""
    if candidate is None or candidate.observation is None:
        return candidate
    return replace(candidate, observation=replace(candidate.observation, output=None))


def _admit_output(
    record: ManagedRunRecord,
    candidate: ManagedObservationCandidate | None,
) -> tuple[StreamDisposition | None, bytes | None, int | None]:
    if candidate is None or candidate.observation is None:
        return None, None, None
    observation = candidate.observation
    if observation.state not in (ManagedObservationState.AVAILABLE, ManagedObservationState.UNAVAILABLE):
        return None, None, None
    try:
        end = decode_managed_job_fact(observation.facts[1][1])
    except (ManagedJobFactError, IndexError):
        return None, None, None
    if not isinstance(end, StreamEndFact):
        return None, None, None

    policy = record.output_policy
    if policy.mode is ManagedOutputMode.CAPTURE:
        output = observation.output
        if (
            observation.state is ManagedObservationState.AVAILABLE
            and end.disposition in (StreamDisposition.COMPLETE_CAPTURE, StreamDisposition.TRUNCATED_CAPTURE)
            and output is not None
            and policy.capture_prefix_bytes is not None
            and end.retained_bytes <= policy.capture_prefix_bytes
        ):
            return end.disposition, output, end.retained_bytes
    elif (
        observation.state is ManagedObservationState.UNAVAILABLE
        and observation.output is None
        and (
            (policy.mode is ManagedOutputMode.DISCARD and end.disposition is StreamDisposition.DISCARDED)
            or (
                policy.mode is ManagedOutputMode.SENSITIVITY_SUPPRESSED
                and end.disposition is StreamDisposition.SUPPRESSED
            )
        )
    ):
        return end.disposition, None, end.retained_bytes
    return None, None, None
