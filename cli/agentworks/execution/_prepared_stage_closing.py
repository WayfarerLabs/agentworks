"""Fixed stage cleanup under previously admitted aggregate closing custody."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import StateError, ValidationError

from ._delivery_custody import LocalDeliveryCustody
from ._file_stage_exchange import FileStageObservationState, stage_cleanup, stage_reconcile
from ._prepared_helper_obligation import decode_prepared_helper_obligation
from .carrier import Dispatch, ExitStatus

if TYPE_CHECKING:
    from ._file_stage_exchange import FileStageCandidateResult
    from ._helper_closure import HelperClosureDelivery
    from ._prepared_helper_stage import PreparedHelperStage
    from ._runtime_prerequisite import HelperClosureExpectation
    from .carrier import Carrier, CarrierIO, CarrierReport, ChannelFeatures, Deadline, PreparedInvocation


@dataclass(slots=True, repr=False)
class _ClosingStageCall:
    base_carrier: Carrier
    carrier: Carrier
    cleanup: bool
    custody: LocalDeliveryCustody = field(default_factory=LocalDeliveryCustody)
    expectation: HelperClosureExpectation | None = None
    delivery: HelperClosureDelivery | None = None
    entered: bool = False
    candidate: FileStageCandidateResult | None = None


class _ClosingCarrier:
    """Private delivery used only by the two fixed cleanup exchanges below."""

    def __init__(self, cleanup: PreparedStageCleanup, call: _ClosingStageCall) -> None:
        self._cleanup, self._call = cleanup, call

    @property
    def features(self) -> ChannelFeatures:
        return self._call.carrier.features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._call.carrier.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        cleanup, call = self._cleanup, self._call
        self.validate(invocation, io=io)
        cleanup._require_cleanup(deadline)
        if cleanup._active is not call or call.entered:
            raise StateError("Prepared stage closing delivery is unavailable")
        cleanup._route(deadline)
        cleanup._require_cleanup(deadline)
        call.entered = True
        return call.carrier.execute(invocation, io=io, deadline=deadline, custody=call.custody)


class PreparedStageCleanup:
    """Parent-retained, serialized closing of one exact prepared input stage.

    A true finish means its cleanup debt is resolved. False retains unknown
    delivery or cleanup debt; later calls may retry removal after exact closure.
    Construction retains only typed cleanup identity and performs no effects.
    """

    def __init__(self, stage: PreparedHelperStage) -> None:
        self._stage = stage
        self._owner = stage._owner
        self._ownership = self._owner.ownership
        self._obligation_id = stage.obligation_id
        self._identity = decode_prepared_helper_obligation(stage._payload)
        self._active: _ClosingStageCall | None = None

    def _check_identity(self) -> None:
        stage = self._stage
        if (
            stage._owner is not self._owner
            or self._owner.ownership != self._ownership
            or stage.obligation_id != self._obligation_id
            or decode_prepared_helper_obligation(stage._payload) != self._identity
            or (stage._bootstrap, stage._runtime, stage.token)
            != (self._identity.bootstrap, self._identity.runtime, self._identity.token)
        ):
            raise StateError("Prepared stage closing identity changed")

    def _require_cleanup(self, deadline: Deadline) -> None:
        stage, owner = self._stage, self._owner
        self._check_identity()
        if deadline.expired:
            raise StateError("Prepared stage closing budget elapsed")
        if (
            stage.cleaned
            or stage.registration_uncertain
            or stage.helper_uncertain
            or not stage._begun
            or (stage._activation_prepared and not stage._activation_settled)
        ):
            raise StateError("Prepared stage closing lacks settled preparation and activation")
        with owner._guard:
            owner._reconcile_transition_locked()
            owner._require_no_active_work_locked()
            if owner._released or not owner._close_requested.is_set():
                raise StateError("Prepared stage closing authority is unavailable")
        row = stage._inspect_registration()
        if row is None or row.state is not LifecycleObligationState.POSSIBLE_EFFECT:
            raise StateError("Prepared stage closing obligation is not admitted")
        call = self._active
        if call is not None and (
            call.base_carrier is not stage._carrier
            or (call.delivery is not None and call.carrier is not call.delivery)
            or (call.delivery is None and call.carrier is not call.base_carrier)
            or (call.expectation is not None and call.expectation.guest != self._identity.bootstrap.guest)
        ):
            raise StateError("Prepared stage closing helper binding changed")

    def _route(self, deadline: Deadline) -> None:
        if self._stage._before_dispatch is not None:
            self._stage._before_dispatch(deadline)

    def _prepare_delivery(self, expectation: HelperClosureExpectation) -> _ClosingCarrier:
        call = self._active
        assert call is not None
        call.expectation = expectation
        if self._stage._new_helper_delivery is not None:
            call.delivery = self._stage._new_helper_delivery(expectation)
            call.carrier = call.delivery
        return _ClosingCarrier(self, call)

    def _settle(self, deadline: Deadline, *, observe: bool) -> bool:
        call = self._active
        assert call is not None
        self._require_cleanup(deadline)
        candidate = call.candidate
        observation = candidate.observation if candidate is not None else None
        if candidate is not None and candidate.dispatch is not Dispatch.NOT_SENT and observation is not None:
            if observation.cleanup_debt is not None:
                self._stage.cleanup_debt = observation.cleanup_debt
            if observation.failure is not None and observation.failure.cleanup_debt is not None:
                self._stage.cleanup_debt = observation.failure.cleanup_debt
        if not call.custody.close(deadline):
            return False
        self._require_cleanup(deadline)
        not_sent = candidate is not None and candidate.dispatch is Dispatch.NOT_SENT
        if call.delivery is not None and call.entered and not not_sent and not call.delivery.closure_proven:
            if not observe:
                return False
            self._route(deadline)
            self._require_cleanup(deadline)
            if not call.delivery.observe_closure(deadline=deadline, custody=call.custody):
                return False
            self._require_cleanup(deadline)
        closed = (
            not call.entered
            or not_sent
            or (
                call.delivery.closure_proven
                if call.delivery is not None
                else candidate is not None
                and candidate.dispatch is Dispatch.SENT
                and candidate.carrier_completion == ExitStatus(0)
            )
        )
        if not closed or not call.custody.settled:
            return False
        if not call.entered:
            self._active = None
            return True
        if (
            call.cleanup
            and candidate is not None
            and self._stage._accepted(candidate, FileStageObservationState.CLEANED)
        ):
            self._stage._resolve_cleanup()
        self._active = None
        return True

    def _dispatch(self, deadline: Deadline, *, cleanup: bool) -> bool:
        self._require_cleanup(deadline)
        assert self._active is None
        stage = self._stage
        self._active = _ClosingStageCall(stage._carrier, stage._carrier, cleanup)
        arguments = stage._arguments(deadline)
        arguments["prepare_delivery"] = self._prepare_delivery
        carrier = _ClosingCarrier(self, self._active)
        if cleanup:
            assert stage.cleanup_debt is not None
            candidate = stage_cleanup(carrier, cleanup_debt=stage.cleanup_debt, **arguments)
        else:
            candidate = stage_reconcile(carrier, **arguments)
        self._active.candidate = candidate
        return self._settle(deadline, observe=False)

    def finish(self, deadline: Deadline) -> bool:
        """Drain retained custody and remove only this stage, under a finite budget."""
        if deadline.expires_at is None or deadline.expired:
            raise ValidationError("Prepared stage closing requires a finite live deadline")
        self._check_identity()
        stage = self._stage
        stage.failed = True
        if stage.helper_uncertain:
            stage.observe_cleanup(deadline)
        stage.retry_bookkeeping()
        if stage.cleaned:
            self._active = None
            return True
        if not stage._begun:
            return True
        if self._active is not None and not self._settle(deadline, observe=True):
            return False
        if stage.cleaned:
            return True
        if not self._owner._close_requested.is_set():
            return stage.cleanup(deadline=deadline)
        if stage.cleanup_debt is None and not self._dispatch(deadline, cleanup=False):
            return False
        if stage.cleanup_debt is None:
            return False
        return self._dispatch(deadline, cleanup=True) and stage.cleaned
