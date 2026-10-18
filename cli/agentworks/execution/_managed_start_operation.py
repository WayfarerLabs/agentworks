"""Core custody for one exact resource-owned or operation-owned managed start."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.db import OperationResourceKind
from agentworks.errors import StateError, ValidationError
from agentworks.operations import OperationAttempt, _is_pre_registration_refusal, release_borrow_after_custody

from ._delivery_custody import LocalDeliveryCustody
from ._fixed_helper_operation import BorrowedFixedHelperCarrier
from ._managed_run_obligation import decode_managed_run_obligation, encode_managed_run_obligation
from ._managed_runs import (
    ManagedLaunchState,
    ManagedRunLifetime,
    ManagedRunOwnerKind,
    ManagedRunRecord,
    ManagedTargetKind,
)
from ._managed_start_exchange import ManagedStartAttempt, ManagedStartCandidate, _PreparedAttempt, start_managed_run
from .carrier import Deadline, Dispatch, ExitStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.operations import OperationBorrow, OperationOwner

    from ._helper_closure import HelperClosureDelivery
    from ._managed_runs import ManagedRunIdentity, ManagedRunRepository
    from ._runtime_prerequisite import HelperClosureExpectation
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


class ManagedStartCustody:
    """Same-process custody of one actual START helper, independent of its receipt.

    The parent publishes this inert object before admission. Continuation retains
    only exact handles and bounded dispatch facts, never preparation or output.
    """

    def __init__(
        self,
        repository: ManagedRunRepository,
        reserved: ManagedRunRecord,
        owner: OperationOwner,
        obligation_id: str,
        route_check: Callable[[Deadline], None] | None,
    ) -> None:
        self._repository = repository
        self._reserved = reserved
        self._owner = owner
        self._ownership = owner.ownership
        self._obligation_id = obligation_id
        self._route_check = route_check
        self._borrow: OperationBorrow | None = None
        self._operation: BorrowedFixedHelperCarrier | None = None
        self._delivery: HelperClosureDelivery | None = None
        self._local = LocalDeliveryCustody()
        self._dispatch = Dispatch.UNKNOWN
        self._completion: ExitStatus | None = None
        self._active = True

    def _capture(self, candidate: ManagedStartCandidate) -> None:
        self._dispatch = candidate.dispatch
        self._completion = candidate.carrier_completion
        operation = self._operation
        if operation is not None and operation.outstanding_attempt is not None:
            self._local = operation.outstanding_attempt.local_delivery

    @property
    def completion_view(self) -> tuple[Dispatch, ExitStatus | None, LocalDeliveryCustody] | None:
        """Original dispatch facts, available only after positive helper closure."""
        operation = self._operation
        if operation is not None and operation.outstanding_attempt is not None:
            self._local = operation.outstanding_attempt.local_delivery
        if self._active or not self._local.settled or not self._closed_helper:
            return None
        dispatch = Dispatch.NOT_SENT if operation is not None and not operation.delivery_started else self._dispatch
        return dispatch, self._completion, self._local

    @property
    def _closed_helper(self) -> bool:
        operation = self._operation
        return operation is not None and (
            not operation.delivery_started
            or self._dispatch is Dispatch.NOT_SENT
            or (self._dispatch is Dispatch.SENT and self._completion == ExitStatus(0))
            or (self._delivery is not None and self._delivery.closure_proven)
        )

    def _validate(self) -> None:
        """Fence continuation to the original borrow, row, run and generation."""
        owner, borrow, operation = self._owner, self._borrow, self._operation
        if self._active or borrow is None or operation is None or owner.ownership != self._ownership:
            raise StateError("Managed start continuation has no inactive exact custody")
        with owner._guard:  # noqa: SLF001
            owner._reconcile_transition_locked()  # noqa: SLF001
            if borrow._closed or owner._active_borrow is not borrow:  # noqa: SLF001
                raise StateError("Managed start continuation no longer owns its borrow")
            attempt = owner._outstanding_attempt  # noqa: SLF001
            if attempt is not None and (
                not isinstance(attempt, OperationAttempt) or attempt._borrow is not borrow  # noqa: SLF001
            ):
                raise StateError("Managed start continuation no longer owns its attempt")
            if (
                operation.outstanding_attempt is not None
                and attempt is not None
                and attempt is not operation.outstanding_attempt
            ):
                raise StateError("Managed start continuation attempt changed")
            # A lost begin reply precedes entry into the actual delivery.
            if operation.outstanding_attempt is None and attempt is not None:
                if operation.delivery_started:
                    raise StateError("Managed start continuation lost its delivery attempt")
                operation.outstanding_attempt = attempt
            if operation.outstanding_attempt is not None:
                self._local = operation.outstanding_attempt.local_delivery
            row = owner._repository.inspect_lifecycle_obligation(self._ownership, self._obligation_id)  # noqa: SLF001
            if row is None:
                if borrow._dispatch_obligation is not None or attempt is not None:  # noqa: SLF001
                    raise StateError("Managed start obligation disappeared")
            else:
                if (
                    row.obligation_kind != MANAGED_START_OBLIGATION_KIND
                    or row.payload_version != MANAGED_START_PAYLOAD_VERSION
                    or row.payload != encode_managed_start_obligation(self._reserved.identity.run_id)
                    or row.payload_revision != 0
                ):
                    raise StateError("Managed start obligation identity changed")
                borrow._dispatch_obligation = row  # noqa: SLF001
        record = self._repository.inspect(self._reserved.identity)
        if record is None or (
            record.spec != self._reserved.spec
            or record.output_policy != self._reserved.output_policy
            or record.created_at != self._reserved.created_at
        ):
            raise StateError("Managed start reservation identity changed")

    def observe_cleanup(self, deadline: Deadline) -> None:
        """Retire local delivery and observe this helper under a fresh route budget."""
        if self._borrow is not None and self._borrow.closed and self.completion_view is not None:
            return
        self._validate()
        if self._closed_helper and self._local.settled:
            return
        if deadline.expires_at is None or deadline.expired:
            raise ValidationError("Managed start cleanup requires a fresh finite deadline")
        if not self._local.close(deadline):
            raise StateError("Managed start local delivery remains unsettled")
        self._validate()
        if not self._closed_helper:
            delivery = self._delivery
            if delivery is None:
                raise StateError("Managed start helper closure remains unknown")
            if self._route_check is not None:
                self._route_check(deadline)
            self._validate()
            if not delivery.observe_closure(deadline=deadline, custody=self._local):
                raise StateError("Managed start helper closure remains unknown")
            self._validate()

    def retry_bookkeeping(self) -> None:
        """Settle exact already-closed custody without replaying START or BEGIN."""
        borrow = self._borrow
        if borrow is not None and borrow.closed:
            return
        self._validate()
        operation = self._operation
        assert borrow is not None and operation is not None
        if not self._closed_helper or not self._local.settled:
            raise StateError("Managed start retains unresolved helper custody")
        if operation.outstanding_attempt is not None:
            operation.settle_helper_closure()
        record = self._repository.inspect(self._reserved.identity)
        assert record is not None
        release_borrow_after_custody(
            borrow,
            retain_effect=borrow.dispatch_obligation_may_be_armed
            and record.launch_state is not ManagedLaunchState.RECEIPT_CONFIRMED,
        )


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
    retain_custody: Callable[[ManagedStartCustody], None] | None = None,
    closure_expectation: HelperClosureExpectation | None = None,
    new_helper_delivery: Callable[[HelperClosureExpectation], HelperClosureDelivery] | None = None,
    route_check: Callable[[Deadline], None] | None = None,
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
    if new_helper_delivery is not None and (retain_custody is None or closure_expectation is None):
        raise ValidationError("Managed start closure delivery requires retained exact custody")
    custody = ManagedStartCustody(repository, reserved, owner, obligation_id, route_check)
    if retain_custody is not None:
        retain_custody(custody)
    if new_helper_delivery is not None and closure_expectation is not None:
        custody._delivery = new_helper_delivery(closure_expectation)
    payload = encode_managed_start_obligation(reserved.identity.run_id)
    prepared.claim(reserved, carrier, deadline)
    try:
        borrow = owner.borrow()
        custody._borrow = borrow
    except BaseException:
        custody._active = False
        prepared.discard()
        raise
    try:
        operation = BorrowedFixedHelperCarrier(carrier, borrow, delivery=custody._delivery)
        custody._operation = operation
    except BaseException as control:
        custody._active = False
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
            if route_check is not None:
                route_check(deadline)

        attempt = start_managed_run(
            repository,
            reserved,
            operation,
            prepared=prepared,
            deadline=deadline,
            before_possible_dispatch=before_possible_dispatch,
            publish_candidate=custody._capture,
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
        if retain_custody is None:
            release_borrow_after_custody(borrow, retain_effect=retained)
        elif custody._closed_helper and custody._local.settled:
            custody._active = False
            custody.retry_bookkeeping()
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
            if retain_custody is None:
                release_borrow_after_custody(borrow, retain_effect=armed_effect)
            else:
                retained = True
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
    finally:
        custody._active = False
