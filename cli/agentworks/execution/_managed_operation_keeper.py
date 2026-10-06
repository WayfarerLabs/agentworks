"""Private retained custody for one operation-owned run's fixed lease helpers.

The caller-thread composition retains this object before admission and publishes
ordinary close intent before draining it. No method resolves start or lifecycle debt.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.db import LifecycleObligationState, OperationResourceKind
from agentworks.errors import StateError, ValidationError

from ._delivery_custody import LocalDeliveryCustody
from ._helper_launcher import _validate_plan
from ._managed_job_protocol import encode_managed_job_fact
from ._managed_lease_exchange import ManagedLeaseCandidate, observe_operation_clock, publish_operation_lease
from ._managed_lease_protocol import ClockObservation, LeasePublication
from ._managed_lease_wire import OperationLease, sampled_lease
from ._managed_observation_exchange import observe_managed_run
from ._managed_run_obligation import encode_managed_run_obligation
from ._managed_runs import ManagedLaunchState, ManagedRunLifetime, ManagedRunOwnerKind, ManagedTargetKind
from ._managed_start_exchange import ManagedStartState
from ._managed_stop_exchange import stop_managed_run
from ._runtime_prerequisite import RuntimeTargetOS
from ._vm_guest_identity_protocol import vm_guest_boot_id
from .carrier import Deadline, Dispatch, ExitStatus

if TYPE_CHECKING:
    from agentworks.operations import LifecycleObligation, OperationOwner

    from ._helper_launcher import IdentityPlan
    from ._managed_observation_exchange import ManagedObservationCandidate
    from ._managed_runs import ManagedRunReceipt, ManagedTargetIdentity
    from ._managed_start_operation import ManagedStartOutcome
    from ._managed_stop_exchange import ManagedStopCandidate
    from ._runtime_prerequisite import RuntimeSelection
    from ._vm_guest_identity_protocol import VMGuestIdentity
    from .carrier import Carrier, CarrierIO, CarrierReport, ChannelFeatures, PreparedInvocation


KEEPER_OBLIGATION_KIND = "managed-operation-keeper"
KEEPER_PAYLOAD_VERSION = 1
_CYCLE_SECONDS = 5.0
_CADENCE_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class InitialOperationLease:
    clock: ManagedLeaseCandidate
    lease: OperationLease | None


@dataclass(frozen=True, slots=True)
class KeeperDrain:
    """Local facts only; drained delivery proves no guest termination or fence."""

    worker_active: bool
    startup_pending: bool
    local_settled: bool

    @property
    def drained(self) -> bool:
        return not self.worker_active and not self.startup_pending and self.local_settled


class _ClosingCarrier:
    """Private helper view, reachable only through the two bound cleanup methods."""

    def __init__(self, keeper: ManagedOperationKeeper) -> None:
        self._keeper = keeper

    @property
    def features(self) -> ChannelFeatures:
        return self._keeper._carrier.features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._keeper._carrier.validate(invocation, io=io)

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self._keeper._require_cleanup(deadline)
        return self._keeper._carrier.execute(invocation, io=io, deadline=deadline, custody=self._keeper._custody)


class _LeaseCarrier:
    """Fence the actual LIVE delivery after helper preparation and validation."""

    def __init__(self, keeper: ManagedOperationKeeper) -> None:
        self._keeper = keeper

    @property
    def features(self) -> ChannelFeatures:
        return self._keeper._carrier.features

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._keeper._carrier.validate(invocation, io=io)

    def execute(
        self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline, custody: LocalDeliveryCustody
    ) -> CarrierReport:
        keeper = self._keeper
        keeper._require_live(deadline)
        keeper._fence()
        keeper._require_live(deadline)
        return keeper._carrier.execute(invocation, io=io, deadline=deadline, custody=custody)


class ManagedOperationKeeper:
    """One dedicated carrier, one retained store, and at most one renewal worker.

    Construction is passive. Caller-thread methods are serialized by the owning
    composition; only LIVE fencing and lease delivery run in the worker. Local
    deadlines cannot preempt a blocked database, guest or native system call.
    """

    def __init__(
        self,
        receipt: ManagedRunReceipt,
        dedicated_carrier: Carrier,
        *,
        owner: OperationOwner,
        obligation_id: str,
        target: ManagedTargetIdentity,
        guest: VMGuestIdentity,
        root_plan: IdentityPlan,
        runtime_selection: RuntimeSelection,
    ) -> None:
        scope = owner.ownership.scope
        if (
            receipt.spec.lifetime is not ManagedRunLifetime.OPERATION
            or receipt.spec.owner.kind is not ManagedRunOwnerKind.OPERATION
            or receipt.spec.owner.owner_id != owner.ownership.operation_id
            or receipt.spec.target != target
            or target.kind is not ManagedTargetKind.VM
            or scope.resource_kind is not OperationResourceKind.VM
            or scope.resource_name != target.name
            or target.boot_id != vm_guest_boot_id(guest)
            or _validate_plan(root_plan).euid != 0
            or runtime_selection.target_os is not RuntimeTargetOS.LINUX
        ):
            raise ValidationError("Operation keeper requires an exact owned Linux VM run and root helper")
        self._receipt = receipt
        self._carrier = dedicated_carrier
        self._owner = owner
        self._obligation_id = obligation_id
        self._guest = guest
        self._plan = root_plan
        self._runtime = runtime_selection
        self._expected_launch = encode_managed_job_fact(receipt)
        self._payload = encode_managed_run_obligation(receipt.identity.run_id)
        self._custody = LocalDeliveryCustody()
        self._obligation: LifecycleObligation | None = None
        self.registration_started = False
        self.admission_uncertain = False
        # Exception objects would retain caller payloads through their tracebacks.
        self.failed = False
        self.initial: InitialOperationLease | None = None
        self.last_clock: ManagedLeaseCandidate | None = None
        self.last_publication: ManagedLeaseCandidate | None = None
        self.last_stop: ManagedStopCandidate | None = None
        self.last_cleanup: ManagedObservationCandidate | None = None
        self.publication_uncertain = False
        self._initial_started = False
        self._start_handoff_used = False
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._startup_guard = threading.Lock()
        self._worker_entered = threading.Event()
        self._worker_done = threading.Event()
        self._worker_permission = False
        self._startup_failed = False
        self._drained = False

    @property
    def obligation(self) -> LifecycleObligation | None:
        """Retained handle, including an uncertain admission, for core disposition."""
        return self._obligation

    def admit(self) -> None:
        """Register before ordinary borrowing, retaining the handle before arming."""
        if self.registration_started or self._stop.is_set():
            raise StateError("Operation keeper admission is one-shot")
        self.registration_started = True
        self.admission_uncertain = True
        try:
            self._obligation = self._owner.register_lifecycle_obligation(
                KEEPER_OBLIGATION_KIND,
                payload_version=KEEPER_PAYLOAD_VERSION,
                payload=self._payload,
                obligation_id=self._obligation_id,
            )
            self._fence()
        except BaseException:
            self.failed = True
            self._stop.set()
            raise
        self.admission_uncertain = False

    def _check_binding(self) -> None:
        obligation = self._obligation
        if obligation is None:
            raise StateError("Operation keeper has no admitted lifecycle custody")
        row = obligation._persisted_obligation
        if (
            row.ownership != self._owner.ownership
            or row.obligation_id != self._obligation_id
            or row.obligation_kind != KEEPER_OBLIGATION_KIND
            or row.payload_version != KEEPER_PAYLOAD_VERSION
            or row.payload != self._payload
            or row.payload_revision != 0
            or row.state is not LifecycleObligationState.POSSIBLE_EFFECT
        ):
            raise StateError("Operation keeper lifecycle binding changed")

    def _require_live(self, deadline: Deadline) -> None:
        if self._stop.is_set() or deadline.expired:
            raise StateError("Operation keeper renewal is closed or expired")
        if self.admission_uncertain:
            raise StateError("Operation keeper admission remains uncertain")
        if not self._custody.settled:
            raise StateError("Operation keeper delivery remains unsettled")

    def _fence(self) -> None:
        if self._stop.is_set() or self._obligation is None:
            raise StateError("Operation keeper is closing or unregistered")
        self._obligation.mark_possible_effect()
        self._check_binding()

    @staticmethod
    def _finite(deadline: Deadline) -> None:
        if deadline.expires_at is None:
            raise ValidationError("Operation keeper requires a finite deadline")

    def _sample(self, deadline: Deadline) -> InitialOperationLease:
        self._require_live(deadline)
        clock = observe_operation_clock(
            _LeaseCarrier(self),
            plan=self._plan,
            guest=self._guest,
            runtime_selection=self._runtime,
            deadline=deadline,
            custody=self._custody,
        )
        self.last_clock = clock
        if not isinstance(clock.result, ClockObservation):
            self._stop.set()
            return InitialOperationLease(clock, None)
        self._require_live(deadline)
        self._fence()
        self._require_live(deadline)
        return InitialOperationLease(clock, sampled_lease(self._expected_launch, clock.result.sampled_ns))

    def sample_initial(self, deadline: Deadline) -> InitialOperationLease:
        """Return the original guest-clock expiry for the future caller-thread start."""
        self._finite(deadline)
        if self._initial_started:
            raise StateError("Operation keeper initial sampling is one-shot")
        self._initial_started = True
        try:
            self.initial = self._sample(deadline)
            return self.initial
        except BaseException:
            self.failed = True
            self._stop.set()
            raise

    def acknowledge_start(self, outcome: ManagedStartOutcome) -> None:
        """Start renewal once, only after exact clean ACK and settled start custody."""
        if self._start_handoff_used:
            raise StateError("Operation keeper start handoff is one-shot")
        self._start_handoff_used = True
        attempt = outcome.attempt
        observation = attempt.candidate.observation if attempt is not None else None
        if (
            self.initial is None
            or self.initial.lease is None
            or self._stop.is_set()
            or attempt is None
            or attempt.record.identity != self._receipt.identity
            or attempt.record.spec != self._receipt.spec
            or attempt.record.launch_state is not ManagedLaunchState.RECEIPT_CONFIRMED
            or outcome.launch_state is not ManagedLaunchState.RECEIPT_CONFIRMED
            or outcome.deadline_exceeded
            or outcome.pending_remote_effects
            or outcome.coordination_uncertain
            or outcome.requires_owner_retention
            or attempt.candidate.dispatch is not Dispatch.SENT
            or attempt.candidate.carrier_completion != ExitStatus(0)
            or attempt.candidate.carrier_failure is not None
            or observation is None
            or observation.state is not ManagedStartState.ACKNOWLEDGED
            or observation.issue is not None
            or observation.launch_fact != self._expected_launch
        ):
            self._stop.set()
            raise StateError("Operation keeper requires a clean exact acknowledged start")
        self._worker = threading.Thread(target=self._renew, name="managed-operation-keeper", daemon=True)
        with self._startup_guard:
            try:
                self._worker.start()
            except BaseException as error:
                self._worker_permission = False
                # This fresh private stdlib start reports RuntimeError when
                # native creation was refused, before the target can exist.
                self._startup_failed = not isinstance(error, RuntimeError)
                self.failed = True
                self._stop.set()
                raise
            self._worker_permission = True

    def _renew(self) -> None:
        self._worker_entered.set()
        try:
            with self._startup_guard:
                if not self._worker_permission:
                    return
            next_start = time.monotonic() + _CADENCE_SECONDS
            while not self._stop.wait(max(0.0, next_start - time.monotonic())):
                started = time.monotonic()
                deadline = Deadline(started + _CYCLE_SECONDS)
                sample = self._sample(deadline)
                if sample.lease is None:
                    return
                self._require_live(deadline)
                # The private adapter fences again after publication preparation.
                self.publication_uncertain = True
                publication = publish_operation_lease(
                    _LeaseCarrier(self),
                    expected_launch=self._expected_launch,
                    lease=sample.lease,
                    plan=self._plan,
                    guest=self._guest,
                    runtime_selection=self._runtime,
                    deadline=deadline,
                    custody=self._custody,
                )
                self.last_publication = publication
                if not isinstance(publication.result, LeasePublication):
                    self._stop.set()
                    return
                self.publication_uncertain = False
                next_start = started + _CADENCE_SECONDS
                if next_start <= time.monotonic():
                    next_start = time.monotonic() + _CADENCE_SECONDS
        except BaseException:
            self.failed = True
            self._stop.set()
        finally:
            self._worker_done.set()

    def drain(self, deadline: Deadline) -> KeeperDrain:
        """Stop LIVE permission, join within budget, then close unused local pipes."""
        self._finite(deadline)
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(deadline.remaining())
        # Interrupted native joins can lose their liveness metadata. The target's
        # own completion remains required before pipe use can be declared ended.
        active = worker is not None and (
            worker.is_alive()
            or ((self._worker_permission or self._worker_entered.is_set()) and not self._worker_done.is_set())
        )
        pending = self._startup_failed and not self._worker_entered.is_set()
        settled = self._custody.settled
        if not active and not pending:
            settled = self._custody.close(deadline)
        facts = KeeperDrain(active, pending, settled)
        self._drained = facts.drained
        return facts

    def _require_cleanup(self, deadline: Deadline) -> None:
        self._finite(deadline)
        if not self._drained or not self._stop.is_set() or deadline.expired or not self._custody.settled:
            raise StateError("Operation keeper cleanup requires completed local drain and live budget")
        self._check_binding()

    def request_stop(self, deadline: Deadline) -> ManagedStopCandidate:
        """Attempt only this run's permanent mutation closure after local drain."""
        self._require_cleanup(deadline)
        try:
            self.last_stop = stop_managed_run(
                _ClosingCarrier(self),
                expected_launch=self._expected_launch,
                plan=self._plan,
                deadline=deadline,
                runtime_selection=self._runtime,
                guest=self._guest,
            )
            return self.last_stop
        except BaseException:
            self.failed = True
            raise

    def observe_cleanup(self, deadline: Deadline) -> ManagedObservationCandidate:
        """Keep native controller observation separate from workload/store facts."""
        self._require_cleanup(deadline)
        try:
            self.last_cleanup = observe_managed_run(
                _ClosingCarrier(self),
                expected_launch=self._expected_launch,
                plan=self._plan,
                deadline=deadline,
                runtime_selection=self._runtime,
                guest=self._guest,
            )
            return self.last_cleanup
        except BaseException:
            self.failed = True
            raise
