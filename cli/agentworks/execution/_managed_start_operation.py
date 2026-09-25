"""Temporary core custody for one resource-owned independent managed start."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.db import OperationResourceKind
from agentworks.errors import ValidationError
from agentworks.operations import _PreRegistrationClosingRefusal, release_borrow_after_custody

from ._fixed_helper_operation import BorrowedFixedHelperCarrier
from ._managed_runs import (
    ManagedLaunchState,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwnerKind,
    ManagedRunRecord,
    ManagedTargetKind,
)
from ._managed_start_exchange import ManagedStartAttempt, start_managed_run
from .carrier import Deadline

if TYPE_CHECKING:
    from agentworks.operations import OperationOwner

    from ._helper_launcher import IdentityPlan
    from ._managed_job_request import ManagedJobRequest
    from ._managed_runs import ManagedRunRepository
    from ._runtime_prerequisite import RuntimeSelection
    from .carrier import Carrier


MANAGED_START_OBLIGATION_KIND = "managed-start"
MANAGED_START_PAYLOAD_VERSION = 1


def encode_managed_start_obligation(run_id: str) -> bytes:
    """Encode only the canonical run identity needed to reconcile dispatch custody."""
    identity = ManagedRunIdentity(run_id)
    return json.dumps({"run_id": identity.run_id, "version": 1}, sort_keys=True, separators=(",", ":")).encode("ascii")


def decode_managed_start_obligation(payload: bytes) -> ManagedRunIdentity:
    """Validate persisted recovery identity at its cross-execution boundary."""
    if type(payload) is not bytes or len(payload) != 57:
        raise ValidationError("Managed start obligation payload is invalid")
    try:
        value = json.loads(payload)
        if type(value) is not dict or set(value) != {"run_id", "version"} or value["version"] != 1:
            raise ValueError
        identity = ManagedRunIdentity(value["run_id"])
        if encode_managed_start_obligation(identity.run_id) != payload:
            raise ValueError
    except (TypeError, ValueError, UnicodeError):
        raise ValidationError("Managed start obligation payload is invalid") from None
    return identity


@dataclass(frozen=True, slots=True, repr=False)
class ManagedStartOutcome:
    """Immediate start facts and independent core custody state."""

    attempt: ManagedStartAttempt | None = field(default=None, repr=False)
    launch_state: ManagedLaunchState | None = None
    deadline_exceeded: bool = False
    pending_remote_effects: bool = False
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False


class ManagedStartControlFact(Exception):
    """Bounded custody attached to escaping start control flow."""

    def __init__(self, outcome: ManagedStartOutcome) -> None:
        self.outcome = outcome
        super().__init__("managed start stopped with retained operation state")


def start_owned_managed_run(
    repository: ManagedRunRepository,
    reserved: ManagedRunRecord,
    carrier: Carrier,
    *,
    request: ManagedJobRequest,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    owner: OperationOwner,
    obligation_id: str,
) -> ManagedStartOutcome:
    """Start once under an already held exact-VM owner; never close that owner."""
    if type(deadline) is not Deadline or deadline.expires_at is None:
        raise ValidationError("Managed start requires one finite deadline")
    if type(reserved) is not ManagedRunRecord:
        raise ValidationError("Managed start requires a reserved run")
    spec = reserved.spec
    scope = owner.ownership.scope
    if (
        reserved.launch_state is not ManagedLaunchState.RESERVED
        or spec.target.kind is not ManagedTargetKind.VM
        or scope.resource_kind is not OperationResourceKind.VM
        or scope.resource_name != spec.target.name
        or spec.lifetime is not ManagedRunLifetime.INDEPENDENT
        or spec.owner.kind is not ManagedRunOwnerKind.RESOURCE
    ):
        raise ValidationError("Managed start requires an exact VM and independent resource owner")
    payload = encode_managed_start_obligation(reserved.identity.run_id)
    borrow = owner.borrow()
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    attempt: ManagedStartAttempt | None = None
    registration_started = False
    retained = False
    try:
        registration_started = True
        borrow.install_dispatch_obligation(
            obligation_id,
            MANAGED_START_OBLIGATION_KIND,
            payload_version=MANAGED_START_PAYLOAD_VERSION,
            payload=payload,
        )
        attempt = start_managed_run(
            repository,
            reserved,
            operation,
            request=request,
            plan=plan,
            deadline=deadline,
            runtime_selection=runtime_selection,
            before_possible_dispatch=borrow.arm_dispatch_obligation,
        )
        operation.settle(attempt.candidate.dispatch, attempt.candidate.carrier_completion)
        confirmed = (
            attempt.record.launch_state is ManagedLaunchState.RECEIPT_CONFIRMED
            and not operation.has_outstanding_attempt
        )
        retained = not confirmed or operation.requires_owner_retention
        outcome = ManagedStartOutcome(
            attempt,
            attempt.record.launch_state,
            deadline.expired,
            operation.pending_remote_effects,
            operation.coordination_uncertain,
            retained,
        )
        release_borrow_after_custody(borrow, retain_effect=retained)
        return outcome
    except _PreRegistrationClosingRefusal:
        borrow.close()
        raise
    except BaseException as control:
        armed_effect = borrow.dispatch_obligation_may_be_armed
        registration_uncertain = registration_started and not borrow.has_installed_dispatch_obligation
        retained = armed_effect or operation.requires_owner_retention or registration_uncertain
        release_failed = False
        try:
            release_borrow_after_custody(borrow, retain_effect=armed_effect)
        except BaseException:
            release_failed = True
        fact = ManagedStartControlFact(
            ManagedStartOutcome(
                None,
                attempt.record.launch_state if attempt is not None else None,
                deadline.expired,
                operation.pending_remote_effects,
                operation.coordination_uncertain
                or operation.has_outstanding_attempt
                or registration_uncertain
                or release_failed,
                retained or release_failed,
            )
        )
        raise control from fact
