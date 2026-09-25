"""Private exact-run managed stop under a caller-held VM operation owner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.operations import _PreRegistrationClosingRefusal, release_borrow_after_custody

from ._fixed_helper_operation import BorrowedFixedHelperCarrier
from ._managed_bound_run import preflight_bound_run
from ._managed_run_obligation import decode_managed_run_obligation, encode_managed_run_obligation
from ._managed_runs import ManagedLaunchState, ManagedRunIdentity, ManagedRunRepository, ManagedTargetIdentity
from ._managed_stop_exchange import ManagedStopCandidate, ManagedStopState, stop_managed_run
from .carrier import Deadline, Dispatch, ExitStatus

if TYPE_CHECKING:
    from agentworks.operations import OperationOwner

    from ._helper_launcher import IdentityPlan
    from ._runtime_prerequisite import RuntimeSelection
    from ._vm_guest_identity_protocol import VMGuestIdentity
    from .carrier import Carrier


MANAGED_STOP_OBLIGATION_KIND = "managed-stop"
MANAGED_STOP_PAYLOAD_VERSION = 1


def encode_managed_stop_obligation(run_id: str) -> bytes:
    """Store only the canonical non-secret run ID for later recovery."""
    return encode_managed_run_obligation(run_id)


def decode_managed_stop_obligation(payload: bytes) -> ManagedRunIdentity:
    """Reject noncanonical or additional persisted recovery data."""
    return decode_managed_run_obligation(payload)


@dataclass(frozen=True, slots=True, repr=False)
class ManagedStopOutcome:
    """One stop attempt, proven response, and temporary obligation custody."""

    candidate: ManagedStopCandidate | None = field(default=None, repr=False)
    state: ManagedStopState | None = None
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class ManagedStopControlFact(Exception):
    """Custody facts attached to original escaping control flow."""

    def __init__(self, outcome: ManagedStopOutcome) -> None:
        self.outcome = outcome
        super().__init__("managed stop stopped with retained operation state")


def stop_bound_managed_run(
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
) -> ManagedStopOutcome:
    """Attempt one stop; caller keeps its owner and any unresolved custody.

    This private seam checks supplied target facts, not production route freshness.
    It neither changes the run row nor retries uncertain delivery.
    """
    if (
        type(obligation_id) is not str
        or len(obligation_id) != 32
        or any(character not in "0123456789abcdef" for character in obligation_id)
    ):
        raise ValidationError("Managed stop requires a fresh canonical obligation ID")
    record, expected_launch = preflight_bound_run(
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
        raise ValidationError("Managed stop requires a reconciled launch receipt")

    payload = encode_managed_stop_obligation(record.identity.run_id)
    borrow = owner.borrow()
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    candidate: ManagedStopCandidate | None = None
    registration_started = False
    release_attempted = False
    try:
        registration_started = True
        borrow.install_dispatch_obligation(
            obligation_id,
            MANAGED_STOP_OBLIGATION_KIND,
            payload_version=MANAGED_STOP_PAYLOAD_VERSION,
            payload=payload,
        )
        candidate = stop_managed_run(
            operation,
            expected_launch=expected_launch,
            plan=root_plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
            guest=guest,
        )
        operation.settle(candidate.dispatch, candidate.carrier_completion)
        state = candidate.observation.state if candidate.observation is not None else None
        proved = candidate.dispatch is Dispatch.NOT_SENT or (
            candidate.dispatch is Dispatch.SENT
            and candidate.carrier_completion == ExitStatus(code=0)
            and state in (ManagedStopState.ACCEPTED, ManagedStopState.TERMINATED)
        )
        resolved = proved and not operation.requires_owner_retention
        retained = not resolved
        outcome = ManagedStopOutcome(
            candidate,
            state if resolved and candidate.dispatch is Dispatch.SENT else None,
            operation.pending_remote_effects or (borrow.dispatch_obligation_may_be_armed and retained),
            operation.coordination_uncertain,
            retained,
        )
        release_attempted = True
        release_borrow_after_custody(borrow, retain_effect=retained)
        return outcome
    except _PreRegistrationClosingRefusal:
        borrow.close()
        raise
    except BaseException as control:
        armed = borrow.dispatch_obligation_may_be_armed
        uncertain_registration = registration_started and not borrow.has_installed_dispatch_obligation
        release_failed = release_attempted
        if not release_attempted:
            try:
                release_borrow_after_custody(borrow, retain_effect=armed)
            except BaseException:
                release_failed = True
        fact = ManagedStopOutcome(
            candidate,
            None,
            operation.pending_remote_effects or armed,
            operation.coordination_uncertain
            or operation.has_outstanding_attempt
            or uncertain_registration
            or release_failed,
            armed or operation.requires_owner_retention or uncertain_registration or release_failed,
        )
        raise control from ManagedStopControlFact(fact)
