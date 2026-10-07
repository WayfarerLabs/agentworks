"""Core custody for one exact resource-owned or operation-owned managed start."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.db import OperationResourceKind
from agentworks.errors import ValidationError
from agentworks.operations import _is_pre_registration_refusal, release_borrow_after_custody

from ._fixed_helper_operation import BorrowedFixedHelperCarrier
from ._managed_run_obligation import decode_managed_run_obligation, encode_managed_run_obligation
from ._managed_runs import (
    ManagedLaunchState,
    ManagedRunLifetime,
    ManagedRunOwnerKind,
    ManagedRunRecord,
    ManagedTargetKind,
)
from ._managed_start_exchange import ManagedStartAttempt, _PreparedAttempt, start_managed_run
from .carrier import Deadline

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.operations import OperationOwner

    from ._managed_runs import ManagedRunIdentity, ManagedRunRepository
    from .carrier import Carrier


MANAGED_START_OBLIGATION_KIND = "managed-start"
MANAGED_START_PAYLOAD_VERSION = 1


def encode_managed_start_obligation(run_id: str) -> bytes:
    """Encode only the canonical run identity needed to reconcile dispatch custody."""
    return encode_managed_run_obligation(run_id)


def decode_managed_start_obligation(payload: bytes) -> ManagedRunIdentity:
    """Validate persisted recovery identity at its cross-execution boundary."""
    return decode_managed_run_obligation(payload)


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
    prepared: _PreparedAttempt,
    deadline: Deadline,
    owner: OperationOwner,
    obligation_id: str,
    before_dispatch: Callable[[], None] | None = None,
) -> ManagedStartOutcome:
    """Start once under an already held exact-VM owner; never close that owner.

    The optional route check runs after the dispatch obligation is armed and
    before possible dispatch is recorded. Its refusal retains armed custody.
    """
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
        or not (
            (spec.lifetime is ManagedRunLifetime.INDEPENDENT and spec.owner.kind is ManagedRunOwnerKind.RESOURCE)
            or (
                spec.lifetime is ManagedRunLifetime.OPERATION
                and spec.owner.kind is ManagedRunOwnerKind.OPERATION
                and spec.owner.owner_id == owner.ownership.operation_id
            )
        )
    ):
        raise ValidationError("Managed start requires an exact VM and matching run owner")
    if type(prepared) is not _PreparedAttempt:
        raise ValidationError("Managed start requires exact preparation")
    payload = encode_managed_start_obligation(reserved.identity.run_id)
    prepared.claim(reserved, carrier, deadline)
    try:
        borrow = owner.borrow()
    except BaseException:
        prepared.discard()
        raise
    try:
        operation = BorrowedFixedHelperCarrier(carrier, borrow)
    except BaseException as control:
        prepared.discard()
        try:
            borrow.close()
        except BaseException:
            raise control from control.__cause__
        raise
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

        def before_possible_dispatch() -> None:
            borrow.arm_dispatch_obligation()
            if before_dispatch is not None:
                before_dispatch()

        attempt = start_managed_run(
            repository,
            reserved,
            operation,
            prepared=prepared,
            deadline=deadline,
            before_possible_dispatch=before_possible_dispatch,
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
    except BaseException as control:
        if _is_pre_registration_refusal(control):
            prepared.discard()
            try:
                borrow.close()
            except BaseException:
                raise control from control.__cause__
            raise
        prepared.discard()
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
        fact.__cause__ = control.__cause__
        raise control from fact
