"""Private exact-run managed disposal under a caller-held VM operation owner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.operations import _is_pre_registration_refusal

from ._fixed_helper_operation import BorrowedFixedHelperCarrier
from ._managed_action_custody import ManagedActionCustody
from ._managed_bound_run import preflight_bound_run
from ._managed_disposal_exchange import DisposalCandidate, DisposalState, dispose_managed_run
from ._managed_run_obligation import decode_managed_run_obligation, encode_managed_run_obligation
from ._managed_runs import ManagedLaunchState, ManagedRunIdentity, ManagedRunRepository, ManagedTargetIdentity
from .carrier import Deadline, Dispatch

if TYPE_CHECKING:
    from agentworks.operations import OperationOwner

    from ._execution_operation import ExecutionOperation
    from ._helper_launcher import IdentityPlan
    from ._runtime_prerequisite import RuntimeSelection
    from ._vm_guest_identity_protocol import VMGuestIdentity
    from .carrier import Carrier


MANAGED_DISPOSAL_OBLIGATION_KIND = "managed-dispose"
MANAGED_DISPOSAL_PAYLOAD_VERSION = 1


def encode_managed_disposal_obligation(run_id: str) -> bytes:
    """Store only the canonical non-secret run ID for later recovery."""
    return encode_managed_run_obligation(run_id)


def decode_managed_disposal_obligation(payload: bytes) -> ManagedRunIdentity:
    """Reject noncanonical or additional persisted recovery data."""
    return decode_managed_run_obligation(payload)


@dataclass(frozen=True, slots=True, repr=False)
class ManagedDisposalOutcome:
    """One disposal attempt, proven response, and temporary obligation custody."""

    candidate: DisposalCandidate | None = field(default=None, repr=False)
    state: DisposalState | None = None
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class ManagedDisposalControlFact(Exception):
    """Custody facts attached to original escaping control flow."""

    def __init__(self, outcome: ManagedDisposalOutcome) -> None:
        self.outcome = outcome
        super().__init__("managed disposal stopped with retained operation state")


def dispose_bound_managed_run(
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
    obligation_id: str,
    execution_operation: ExecutionOperation | None = None,
) -> ManagedDisposalOutcome:
    """Attempt one disposal; caller keeps its owner and unresolved custody.

    This private seam checks supplied target facts, not production route freshness.
    It neither changes the run row nor retries uncertain delivery.
    """
    if (
        type(obligation_id) is not str
        or len(obligation_id) != 32
        or any(character not in "0123456789abcdef" for character in obligation_id)
    ):
        raise ValidationError("Managed disposal requires a fresh canonical obligation ID")
    record, expected_launch = preflight_bound_run(
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
    if record.launch_state is not ManagedLaunchState.RECEIPT_CONFIRMED:
        raise ValidationError("Managed disposal requires a reconciled launch receipt")

    payload = encode_managed_disposal_obligation(record.identity.run_id)
    borrow = owner.borrow()
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    custody = ManagedActionCustody(borrow, operation)
    candidate: DisposalCandidate | None = None
    try:
        custody.register(
            obligation_id, MANAGED_DISPOSAL_OBLIGATION_KIND, payload, payload_version=MANAGED_DISPOSAL_PAYLOAD_VERSION
        )
        candidate = dispose_managed_run(
            operation,
            expected_launch=expected_launch,
            plan=root_plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
            guest=guest,
        )
        state = candidate.observation.state if candidate.observation is not None else None
        custody_fact = custody.settle_and_release(
            candidate.dispatch,
            candidate.carrier_completion,
            accepted_response=state in (DisposalState.NOT_READY, DisposalState.DISPOSED),
        )
        return ManagedDisposalOutcome(
            candidate,
            state if not custody_fact.requires_owner_retention and candidate.dispatch is Dispatch.SENT else None,
            custody_fact.pending_remote_effects,
            custody_fact.coordination_uncertain,
            custody_fact.requires_owner_retention,
        )
    except BaseException as control:
        if _is_pre_registration_refusal(control):
            custody.close_pre_registration_refusal()
            raise
        custody_fact = custody.escaped()
        fact = ManagedDisposalOutcome(
            candidate,
            None,
            custody_fact.pending_remote_effects,
            custody_fact.coordination_uncertain,
            custody_fact.requires_owner_retention,
        )
        raise control from ManagedDisposalControlFact(fact)
