"""Retained caller-thread start composition for one private OPERATION run.

Core retains this instance before reservation. Closing, recovery and aggregate
disposition remain with that core; a returned start outcome is only start evidence.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError

from ._managed_job_protocol import encode_managed_job_fact
from ._managed_operation_keeper import ManagedOperationKeeper
from ._managed_request_adapter import _ManagedBody
from ._managed_runs import ManagedLaunchState
from ._managed_start_exchange import ManagedStartState, prepare_managed_start
from ._managed_start_operation import ManagedStartControlFact, ManagedStartOutcome, start_owned_managed_run

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.operations import OperationOwner

    from ._helper_launcher import IdentityPlan
    from ._managed_runs import ManagedRunReceipt, ManagedRunRecord, ManagedRunRepository, ManagedTargetIdentity
    from ._managed_start_exchange import _PreparedAttempt
    from ._runtime_prerequisite import RuntimeSelection
    from ._vm_guest_identity_protocol import VMGuestIdentity
    from .carrier import Carrier, Deadline


class ManagedOperationRun:
    """One planned receipt, one reservation attempt and one owned start.

    Construction is passive. Caller bytes live only in the supplied frozen body
    and the temporary start preparation, never in retained run or ledger state.
    """

    def __init__(
        self,
        repository: ManagedRunRepository,
        receipt: ManagedRunReceipt,
        start_carrier: Carrier,
        dedicated_keeper_carrier: Carrier,
        *,
        owner: OperationOwner,
        start_obligation_id: str,
        keeper_obligation_id: str,
        target: ManagedTargetIdentity,
        guest: VMGuestIdentity,
        root_plan: IdentityPlan,
        runtime_selection: RuntimeSelection,
    ) -> None:
        self._caller_thread = threading.current_thread()
        if start_carrier is dedicated_keeper_carrier or start_obligation_id == keeper_obligation_id:
            raise ValidationError("Operation run requires dedicated delivery and distinct obligation identities")
        self.receipt = receipt
        self.keeper = ManagedOperationKeeper(
            receipt,
            dedicated_keeper_carrier,
            owner=owner,
            obligation_id=keeper_obligation_id,
            target=target,
            guest=guest,
            root_plan=root_plan,
            runtime_selection=runtime_selection,
        )
        self._repository = repository
        self._carrier = start_carrier
        self._owner = owner
        self._start_obligation_id = start_obligation_id
        self._guest = guest
        self._plan = root_plan
        self._runtime = runtime_selection
        self._expected_launch = encode_managed_job_fact(receipt)
        self._started = False
        self.reservation_started = False
        self.reservation_uncertain = False
        self.reserved: ManagedRunRecord | None = None
        self.reservation_observation: ManagedRunRecord | None = None
        self.start_outcome: ManagedStartOutcome | None = None
        self.control_escaped = False
        self._prepared: _PreparedAttempt | None = None

    def _matches_reservation(self, record: ManagedRunRecord, body: _ManagedBody) -> bool:
        return (
            record.identity == self.receipt.identity
            and record.spec == self.receipt.spec
            and record.output_policy == body.output_policy
            and record.launch_state is ManagedLaunchState.RESERVED
        )

    def _reserve(self, body: _ManagedBody) -> None:
        self.reservation_started = True
        self.reservation_uncertain = True
        try:
            record = self._repository.reserve(
                self.receipt.spec, output_policy=body.output_policy, identity=self.receipt.identity
            )
        except BaseException:
            try:
                observed = self._repository.inspect(self.receipt.identity)
                self.reservation_observation = observed
                if observed is None:
                    self.reservation_uncertain = False
                elif self._matches_reservation(observed, body):
                    self.reserved = observed
                    self.reservation_uncertain = False
            except BaseException:
                # Failed observation cannot erase the original reserve exception.
                pass
            raise
        self.reservation_observation = record
        if not self._matches_reservation(record, body):
            raise StateError("Operation reservation does not match planned run")
        self.reserved = record
        self.reservation_uncertain = False

    def start(
        self,
        body: _ManagedBody,
        deadline: Deadline,
        *,
        before_dispatch: Callable[[], None] | None = None,
    ) -> ManagedStartOutcome | None:
        """Reserve before keeper effects, then start once with its actual sample.

        None means no start was attempted after a refused clock observation;
        the keeper retains that raw clock candidate. Escaping control preserves
        this instance's reservation, keeper and independent start custody.
        """
        if threading.current_thread() is not self._caller_thread:
            raise StateError("Operation run start requires its originating caller thread")
        if self._started:
            raise StateError("Operation run start is one-shot")
        self._started = True
        if type(body) is not _ManagedBody or body.request.launch != self._expected_launch:
            raise ValidationError("Operation body does not match planned receipt")
        if deadline.expires_at is None or deadline.expired:
            raise ValidationError("Operation run start requires a live finite deadline")
        try:
            self._reserve(body)
            self.keeper.admit()
            initial = self.keeper.sample_initial(deadline)
            if initial.lease is None:
                return None
            request = replace(body.request, operation_lease=initial.lease)
            self._prepared = prepare_managed_start(
                self._carrier,
                self.receipt.identity,
                self.receipt.spec,
                body.output_policy,
                request,
                self._plan,
                deadline,
                self._runtime,
                self._guest,
            )
            assert self.reserved is not None
            self.start_outcome = start_owned_managed_run(
                self._repository,
                self.reserved,
                self._carrier,
                prepared=self._prepared,
                deadline=deadline,
                owner=self._owner,
                obligation_id=self._start_obligation_id,
                before_dispatch=before_dispatch,
            )
            outcome = self.start_outcome
            observation = outcome.attempt.candidate.observation if outcome.attempt is not None else None
            if (
                observation is not None
                and observation.state is ManagedStartState.ACKNOWLEDGED
                and not outcome.deadline_exceeded
                and not outcome.pending_remote_effects
                and not outcome.coordination_uncertain
                and not outcome.requires_owner_retention
            ):
                self.keeper.acknowledge_start(outcome)
            return outcome
        except BaseException as control:
            self.control_escaped = True
            if isinstance(control.__cause__, ManagedStartControlFact):
                self.start_outcome = control.__cause__.outcome
            raise
        finally:
            prepared = self._prepared
            self._prepared = None
            if prepared is not None:
                prepared.discard()
