"""Private exact-run observation under a caller-held VM operation owner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.db import OperationResourceKind
from agentworks.errors import ValidationError
from agentworks.operations import OperationOwner

from ._fixed_helper_operation import BorrowedFixedHelperCarrier
from ._helper_launcher import IdentityPlan, _validate_plan
from ._managed_job_protocol import (
    ManagedJobFactError,
    StreamDisposition,
    StreamEndFact,
    decode_managed_job_fact,
    encode_managed_job_fact,
    fact_matches_receipt,
)
from ._managed_job_store import FactName, Stream
from ._managed_observation_exchange import (
    ManagedObservationCandidate,
    ManagedObservationState,
    observe_managed_run,
    read_managed_output,
)
from ._managed_runs import (
    ManagedOutputMode,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunRepository,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from ._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from ._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from .carrier import Carrier, Deadline

if TYPE_CHECKING:
    from ._managed_runs import ManagedRunRecord


@dataclass(frozen=True, slots=True, repr=False)
class ManagedObserveOutcome:
    """Raw one-attempt observation and explicit operation custody state."""

    candidate: ManagedObservationCandidate | None = field(default=None, repr=False)
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class ManagedObserveControlFact(Exception):
    """Custody facts attached as the cause of escaping control flow."""

    def __init__(self, outcome: ManagedObserveOutcome) -> None:
        self.outcome = outcome
        super().__init__("managed observation stopped with retained operation state")


@dataclass(frozen=True, slots=True, repr=False)
class ManagedReadOutputOutcome:
    """One raw attempt and its policy-admitted selected stream, if any."""

    attempt: ManagedObserveOutcome
    disposition: StreamDisposition | None = None
    output: bytes | None = field(default=None, repr=False)

    @property
    def accepted(self) -> bool:
        return self.disposition is not None


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
) -> ManagedObserveOutcome:
    """Read one persisted independent VM run under borrowed dispatch custody.

    The caller supplies freshly prepared target and guest facts and retains
    responsibility for route freshness. This does not reconcile launch state,
    reduce output policy, or release the caller's operation owner.
    """
    _, _, outcome = _bound_exchange(
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
    )
    return outcome


def read_bound_managed_output(
    repository: ManagedRunRepository,
    identity: ManagedRunIdentity,
    *,
    stream: Stream,
    target: ManagedTargetIdentity,
    guest: VMGuestIdentity,
    root_plan: IdentityPlan,
    carrier: Carrier,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    owner: OperationOwner,
) -> ManagedReadOutputOutcome:
    """Read one exact closed stream and admit it under persisted output policy."""
    if type(stream) is not Stream:
        raise ValidationError("Managed output requires one selected stream")
    record, receipt, attempt = _bound_exchange(
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
    )
    try:
        disposition, output = _admit_output(record, receipt, stream, attempt)
    except BaseException as control:
        raise control from ManagedObserveControlFact(attempt)
    return ManagedReadOutputOutcome(attempt, disposition, output)


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
) -> tuple[ManagedRunRecord, ManagedRunReceipt, ManagedObserveOutcome]:
    if (
        type(identity) is not ManagedRunIdentity
        or type(target) is not ManagedTargetIdentity
        or target.kind is not ManagedTargetKind.VM
        or type(guest) is not VMGuestIdentity
        or target.boot_id != vm_guest_boot_id(guest)
        or type(runtime_selection) is not RuntimeSelection
        or runtime_selection.target_os is not RuntimeTargetOS.LINUX
        or type(deadline) is not Deadline
        or deadline.expires_at is None
        or deadline.expired
        or not isinstance(owner, OperationOwner)
        or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
        or owner.ownership.scope.resource_name != target.name
    ):
        raise ValidationError("Managed observation requires an owned exact Linux VM and finite deadline")
    if _validate_plan(root_plan).euid != 0:
        raise ValidationError("Managed observation requires a root helper plan")

    record = repository.inspect(identity)
    if (
        record is None
        or record.identity != identity
        or record.spec.target != target
        or record.spec.target.kind is not ManagedTargetKind.VM
        or record.spec.lifetime is not ManagedRunLifetime.INDEPENDENT
        or record.spec.owner.kind is not ManagedRunOwnerKind.RESOURCE
    ):
        raise ValidationError("Managed observation requires an exact independent VM reservation")
    receipt = ManagedRunReceipt(record.identity, record.identity.unit_name, record.spec)
    expected_launch = encode_managed_job_fact(receipt)

    borrow = owner.borrow()
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    candidate: ManagedObservationCandidate | None = None
    try:
        if stream is None:
            candidate = observe_managed_run(
                operation,
                expected_launch=expected_launch,
                plan=root_plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                guest=guest,
            )
        else:
            candidate = read_managed_output(
                operation,
                expected_launch=expected_launch,
                stream=stream,
                plan=root_plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                guest=guest,
            )
        operation.settle(candidate.dispatch, candidate.carrier_completion)
    except BaseException as control:
        custody, _ = operation.release(control_escaped=True)
        outcome = ManagedObserveOutcome(
            candidate,
            custody.pending_remote_effects,
            custody.coordination_uncertain,
            custody.requires_owner_retention,
        )
        raise control from ManagedObserveControlFact(outcome)

    custody, release_error = operation.release()
    outcome = ManagedObserveOutcome(
        candidate, custody.pending_remote_effects, custody.coordination_uncertain, custody.requires_owner_retention
    )
    if release_error is not None:
        raise release_error from ManagedObserveControlFact(outcome)
    return record, receipt, outcome


def _admit_output(
    record: ManagedRunRecord,
    receipt: ManagedRunReceipt,
    stream: Stream,
    attempt: ManagedObserveOutcome,
) -> tuple[StreamDisposition | None, bytes | None]:
    candidate = attempt.candidate
    if candidate is None or candidate.observation is None:
        return None, None
    observation = candidate.observation
    end_name = FactName.STDOUT_END if stream is Stream.STDOUT else FactName.STDERR_END
    facts = observation.facts
    if (
        len(facts) != 2
        or facts[0] != (FactName.LAUNCH, encode_managed_job_fact(receipt))
        or facts[1][0] is not end_name
    ):
        return None, None
    try:
        end = decode_managed_job_fact(facts[1][1])
    except ManagedJobFactError:
        return None, None
    if not isinstance(end, StreamEndFact) or end.stream.value != stream.value or not fact_matches_receipt(end, receipt):
        return None, None

    policy = record.output_policy
    if policy.mode is ManagedOutputMode.CAPTURE:
        output = observation.output
        if (
            observation.state is ManagedObservationState.AVAILABLE
            and end.disposition in (StreamDisposition.COMPLETE_CAPTURE, StreamDisposition.TRUNCATED_CAPTURE)
            and output is not None
            and policy.capture_prefix_bytes is not None
            and end.retained_bytes <= policy.capture_prefix_bytes
            and len(output) == end.retained_bytes
        ):
            return end.disposition, output
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
        return end.disposition, None
    return None, None
