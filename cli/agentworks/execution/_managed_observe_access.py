"""Private exact-run observation under a caller-held VM operation owner."""

from __future__ import annotations

from dataclasses import dataclass, field

from agentworks.db import OperationResourceKind
from agentworks.errors import ValidationError
from agentworks.operations import OperationOwner, release_borrow_after_custody

from ._fixed_helper_operation import BorrowedFixedHelperCarrier
from ._helper_launcher import IdentityPlan, _validate_plan
from ._managed_job_protocol import encode_managed_job_fact
from ._managed_observation_exchange import ManagedObservationCandidate, observe_managed_run
from ._managed_runs import (
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
    expected_launch = encode_managed_job_fact(
        ManagedRunReceipt(record.identity, record.identity.unit_name, record.spec)
    )

    borrow = owner.borrow()
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    candidate: ManagedObservationCandidate | None = None
    try:
        candidate = observe_managed_run(
            operation,
            expected_launch=expected_launch,
            plan=root_plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
            guest=guest,
        )
        operation.settle(candidate.dispatch, candidate.carrier_completion)
    except BaseException as control:
        retained = operation.requires_owner_retention
        release_failed = False
        try:
            release_borrow_after_custody(borrow, retain_effect=retained)
        except BaseException:
            release_failed = True
        outcome = ManagedObserveOutcome(
            candidate,
            operation.pending_remote_effects,
            operation.coordination_uncertain or operation.has_outstanding_attempt or release_failed,
            retained or release_failed,
        )
        raise control from ManagedObserveControlFact(outcome)

    retained = operation.requires_owner_retention
    try:
        release_borrow_after_custody(borrow, retain_effect=retained)
    except BaseException as control:
        outcome = ManagedObserveOutcome(candidate, operation.pending_remote_effects, True, True)
        raise control from ManagedObserveControlFact(outcome)
    return ManagedObserveOutcome(
        candidate, operation.pending_remote_effects, operation.coordination_uncertain, retained
    )
