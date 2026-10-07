"""Retained caller-thread start composition for one private OPERATION run.

Core retains this instance before reservation. Closing, recovery and aggregate
disposition remain with that core; a returned start outcome is only start evidence.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from agentworks.errors import StateError, ValidationError

from ._managed_job_protocol import encode_managed_job_fact
from ._managed_job_store import FactName
from ._managed_observation_exchange import ManagedObservationCandidate, ManagedObservationState
from ._managed_observation_protocol import ControllerState, ManagedObservationError, checked_controller, checked_fact
from ._managed_operation_keeper import ManagedOperationKeeper, _clean_start_acknowledged
from ._managed_request_adapter import _ManagedBody
from ._managed_runs import ManagedLaunchState
from ._managed_start_exchange import prepare_managed_start
from ._managed_start_operation import ManagedStartControlFact, ManagedStartOutcome, start_owned_managed_run
from ._managed_stop_exchange import ManagedStopState
from .carrier import Dispatch, ExitStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.operations import OperationOwner

    from ._helper_launcher import IdentityPlan
    from ._managed_disposal_access import ManagedDisposalOutcome
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
        self.cleanup_complete = False
        self.terminal_observation: ManagedObservationCandidate | None = None
        self.disposal_confirmed = False
        self.disposal_attempted = False
        self.disposal_obligation_id = uuid4().hex
        self.disposal_outcome: ManagedDisposalOutcome | None = None

    def terminal_proved(self, candidate: ManagedObservationCandidate | None) -> bool:
        """Positive resource closure is independent of application exit precision."""
        observation = None if candidate is None else candidate.observation
        required = {FactName.LAUNCH, FactName.STDOUT_END, FactName.STDERR_END, FactName.BOUNDARY_EMPTY}
        if (
            not self.acknowledged
            or candidate is None
            or candidate.dispatch is not Dispatch.SENT
            or candidate.carrier_completion != ExitStatus(0)
            or candidate.carrier_failure is not None
            or observation is None
            or observation.state is not ManagedObservationState.OBSERVED
            or not required.issubset({name for name, _ in observation.facts})
            or observation.controller is None
            or observation.controller.state not in {ControllerState.EXITED, ControllerState.ABSENT}
        ):
            return False
        try:
            for name, data in observation.facts:
                checked_fact(name, data, self._expected_launch)
            checked_controller(observation.controller, self._expected_launch)
        except ManagedObservationError:
            return False
        return True

    @property
    def acknowledged(self) -> bool:
        return _clean_start_acknowledged(self.start_outcome, self.receipt, self._expected_launch)

    def finish_cleanup(self, deadline: Deadline) -> None:
        """Settle only this run after its keeper's separate local drain.

        Original uncertain start debt is never resolved here. Store termination
        facts and native controller termination remain independent requirements.
        """
        obligation = self.keeper.obligation
        if self.reservation_uncertain or self.keeper.admission_uncertain:
            raise StateError("Managed run admission remains uncertain")
        if not self.keeper.registration_started and self.start_outcome is None:
            self.cleanup_complete = True
        if not self.cleanup_complete and not self.acknowledged:
            record = self._repository.inspect(self.receipt.identity)
            clock = self.keeper.last_clock
            if (
                record is not None
                and record.launch_state
                in {
                    ManagedLaunchState.RESERVED,
                    ManagedLaunchState.NOT_LAUNCHED,
                }
                and (
                    not self.keeper._initial_started  # noqa: SLF001
                    or (
                        clock is not None
                        and (
                            clock.dispatch is Dispatch.NOT_SENT
                            or (clock.dispatch is Dispatch.SENT and clock.carrier_completion == ExitStatus(0))
                        )
                    )
                )
            ):
                self.cleanup_complete = True
        if not self.cleanup_complete:
            stop = self.keeper.last_stop
            if (
                stop is None
                or stop.observation is None
                or stop.observation.state
                not in {
                    ManagedStopState.ACCEPTED,
                    ManagedStopState.TERMINATED,
                }
            ):
                stop = self.keeper.request_stop(deadline)
            if stop.observation is None or stop.observation.state not in {
                ManagedStopState.ACCEPTED,
                ManagedStopState.TERMINATED,
            }:
                raise StateError("Managed run permanent mutation closure is unproved")
            candidate = self.keeper.observe_cleanup(deadline)
            if not self.terminal_proved(candidate):
                raise StateError("Managed run workload, streams or controller remain unproved")
            self.terminal_observation = candidate
            self.cleanup_complete = True
        if obligation is not None:
            obligation.resolve()

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
            if self.acknowledged:
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
