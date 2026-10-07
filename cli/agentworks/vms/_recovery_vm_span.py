"""Caller-retained availability for private existing-VM recovery work.

The first factory supports administrative WSL2 recovery only. Fresh READY and
prepared guest facts provide no predecessor dispatch or native drain evidence.
Individual recovery adapters still own their exact concrete attempts.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from threading import TIMEOUT_MAX, Lock, get_ident
from typing import TYPE_CHECKING

from agentworks.capabilities.base import ScopeLevel
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import OperationClaimState, OperationResourceKind, VMStatus
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._recovery_guest_preparation import RecoveryGuestPreparationBatch
from agentworks.execution._target_identity import TargetIdentityStatus, _validate_inputs
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence, HandleSettlement, HostClientStatus, JobAssignment
from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation
from agentworks.execution.carrier import Deadline
from agentworks.vms.identity import validate_vm_instance_marker
from agentworks.vms.target_preparation import VMTargetPreparationStatus

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform.base import VMPlatform
    from agentworks.db import Database, VMRow
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._managed_runs import ManagedTargetIdentity
    from agentworks.execution._recovery_guest_preparation import RecoveryGuestPreparation
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
    from agentworks.execution._wsl2_lifecycle import GuestAnchorObserver, OwnedHostClient
    from agentworks.execution.carrier import (
        Carrier,
        CarrierIO,
        CarrierReport,
        ChannelFeatures,
        PreparedInvocation,
    )
    from agentworks.operations import OperationOwner


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryVMPreparedContext:
    """Exact prepared facts and a carrier usable only inside its bound action."""

    target: ManagedTargetIdentity
    guest: VMGuestIdentity
    ordinary_plan: IdentityPlan
    root_plan: IdentityPlan
    runtime_selection: RuntimeSelection
    carrier: Carrier = field(repr=False)
    _span: RecoveryVMSpan = field(repr=False)


class RecoveryVMSpanControlFact(Exception):
    """Keep actual span custody reachable when an original exception escapes."""

    def __init__(self, span: RecoveryVMSpan) -> None:
        self.span = span
        self.preparation = span.preparation
        super().__init__("recovery VM span retains availability custody")


@dataclass(slots=True)
class _Action:
    thread: int
    deadline: Deadline
    active: bool = True


class _ActionCarrier:
    """Pin the actual carrier to one locally protected, non-escaping action."""

    def __init__(self, span: RecoveryVMSpan, carrier: Carrier, action: _Action | None) -> None:
        self._span = span
        self._carrier = carrier
        self._action = action

    @property
    def features(self) -> ChannelFeatures:
        return self._carrier.features

    def _require_action(self) -> None:
        action = self._action
        if (
            action is None
            or not action.active
            or action.thread != get_ident()
            or action.deadline.expired
            or self._span._closed
        ):
            raise StateError("Recovery carrier requires its live bound action")

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        self._require_action()
        self._carrier.validate(invocation, io=io)

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody,
    ) -> CarrierReport:
        if not custody.settled:
            raise StateError("Recovery carrier retains unsettled local delivery")
        self._require_action()
        action = self._action
        assert action is not None and action.deadline.expires_at is not None
        if (
            type(deadline) is not Deadline
            or deadline.expires_at is None
            or deadline.expires_at > action.deadline.expires_at
        ):
            raise ValidationError("Recovery carrier deadline exceeds its bound action budget")
        self._span._revalidate_action(deadline)
        return self._carrier.execute(invocation, io=io, deadline=deadline, custody=custody)


class RecoveryVMSpan:
    """Retain one renewed hold under the caller's existing sealed recovery owner.

    Construct and retain before opening. Actions serialize against close and
    expose fresh inert-after-use views. Close stops only this span's admission;
    the caller still dispositions debts and finalizes the aggregate owner.
    """

    def __init__(
        self,
        db: Database,
        vm_name: str,
        platform: VMPlatform,
        ctx: RunContext,
        *,
        owner: OperationOwner,
        hold_id: str,
        preparation_id: str,
        workload_account: str,
        native: OwnedHostClient | None = None,
        observer: GuestAnchorObserver | None = None,
    ) -> None:
        if hold_id == preparation_id or any(
            len(value) != 32 or any(char not in "0123456789abcdef" for char in value)
            for value in (hold_id, preparation_id)
        ):
            raise ValidationError("Recovery span requires independent fresh support identifiers")
        self._db = db
        self._vm_name = vm_name
        self._platform = platform
        self._ctx = ctx
        self._owner = owner
        self._local_delivery = LocalDeliveryCustody()
        self._hold_id = hold_id
        self._preparation_id = preparation_id
        self._workload_account = workload_account
        self._native = native
        self._observer = observer
        self._transition = Lock()
        self._started = False
        self._closing = False
        self._closed = False
        self._hold_started = False
        self._selected: WSL2OwnedOperation | None = None
        self._batch: RecoveryGuestPreparationBatch | None = None
        self._prepared: RecoveryVMPreparedContext | None = None

    @property
    def preparation(self) -> RecoveryGuestPreparation | None:
        return self._batch.preparation if self._batch is not None else None

    @property
    def requires_owner_retention(self) -> bool:
        """Report this span's local custody, not aggregate predecessor debt."""
        return not self._closed and (self._hold_started or not self._local_delivery.settled)

    def _acquire(self, deadline: Deadline) -> None:
        try:
            if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
                raise ValidationError("Recovery span requires a live finite deadline")
            remaining = deadline.remaining()
            assert remaining is not None
            if not self._transition.acquire(timeout=min(remaining, TIMEOUT_MAX)):
                raise TimeoutError("Recovery span transition exceeded its deadline")
            if deadline.expired:
                self._transition.release()
                raise TimeoutError("Recovery span transition exceeded its deadline")
        except BaseException as control:
            raise control from RecoveryVMSpanControlFact(self)

    def _require_owner(self, *, idle: bool = False) -> None:
        owner = self._owner
        scope = owner.ownership.scope
        if (
            scope.resource_kind is not OperationResourceKind.VM
            or scope.resource_name != self._vm_name
            or not owner._recovery_owner  # noqa: SLF001
            or owner._repository._controller_identity is not self._db  # noqa: SLF001
        ):
            raise ValidationError("Recovery span requires this database's exact recovered VM owner")
        with owner._guard:  # noqa: SLF001
            owner._require_dispatch_admission_locked()  # noqa: SLF001
            if idle:
                owner._require_no_active_work_locked()  # noqa: SLF001
        claim = self._db.operations.inspect(scope)
        if (
            claim is None
            or claim.ownership != owner.ownership
            or claim.obligations_sealed_at is None
            or claim.state is OperationClaimState.RESOLVED
        ):
            raise StateError("Recovery span ownership is stale or unavailable")

    def _fresh_vm(self, deadline: Deadline, *, action: bool = False) -> VMRow:
        vm = self._db.get_vm(self._vm_name)
        if vm is None or vm.site != self._platform.site_name:
            raise StateError("Recovery span requires the current VM and selected site")
        scope = self._ctx.operation_scope
        if scope is not None and (scope.level is not ScopeLevel.VM or scope.vm != vm.name):
            raise ValidationError("Recovery span requires the selected VM context")
        validate_vm_instance_marker(vm.instance_marker)
        if self._workload_account != vm.admin_username:
            raise ValidationError("WSL2 recovery span requires the explicit administrative account")
        selected = self._selected
        if selected is not None:
            pinned = selected._vm  # noqa: SLF001
            if (
                pinned.site != vm.site
                or pinned.admin_username != vm.admin_username
                or pinned.instance_marker != vm.instance_marker
                or pinned.platform_metadata != vm.platform_metadata
            ):
                raise StateError("Recovery span persisted VM identity changed")
        power = self._platform.observe_execution_power(vm, self._ctx, deadline=deadline, custody=self._local_delivery)
        if deadline.expired or type(power) is not VMStatus or power not in (VMStatus.RUNNING, VMStatus.STOPPED):
            raise StateError("Recovery span power observation is unavailable")
        if power is VMStatus.STOPPED and (vm.operator_stopped or action):
            raise StateError("Recovery span cannot activate this stopped VM")
        return vm

    def open(self, deadline: Deadline) -> RecoveryVMPreparedContext:
        """Select, retain, admit READY, then prepare the exact guest and accounts."""
        self._acquire(deadline)
        try:
            if self._started or self._closing:
                raise StateError("Recovery VM span is single-use")
            self._started = True
            self._require_owner(idle=True)
            if not isinstance(self._platform, WSL2Platform):
                raise StateError("Recovery VM span is unavailable on this platform")
            vm = self._fresh_vm(deadline)
            if any(
                self._owner.inspect_lifecycle_obligation(identity) is not None
                for identity in (self._hold_id, self._preparation_id)
            ):
                raise StateError("Recovery span support identifiers must be fresh")
            self._selected = WSL2OwnedOperation.from_platform(
                vm,
                self._platform,
                self._ctx,
                owner=self._owner,
                deadline=deadline,
                config=self._ctx.config,
                native=self._native,
                observer=self._observer,
                provider_custody=self._local_delivery,
            )
            selected = self._selected
            if selected is None:
                raise StateError("Recovery span native route is unavailable")
            binding = selected.binding
            self._batch = RecoveryGuestPreparationBatch(
                binding, self._owner, self._preparation_id, provider_custody=self._local_delivery
            )
            _validate_inputs(
                delivery_account=binding.delivery_account,
                workload_account=self._workload_account,
                include_elevated=True,
                runtime_selection=binding.runtime_selection,
                deadline=deadline,
                owner=self._owner,
            )
            current = self._fresh_vm(deadline)
            selected.require_selected_route(deadline)
            self._require_owner(idle=True)
            self._hold_started = True
            ready = selected.hold.start_recovery(deadline, obligation_id=self._hold_id)
            selected.ready = ready
            self._require_ready_hold(selected, deadline)
            self._fresh_vm(deadline, action=True)
            preparation = self._batch.prepare(
                current,
                self._platform,
                self._ctx,
                selected._selected_locator,  # noqa: SLF001
                workload_account=self._workload_account,
                include_elevated=True,
                deadline=deadline,
            )
            guest_preparation, identity = preparation.guest, preparation.identity
            if (
                preparation.requires_owner_retention
                or guest_preparation is None
                or guest_preparation.status is not VMTargetPreparationStatus.PREPARED
                or identity is None
                or identity.status is not TargetIdentityStatus.PREPARED
                or identity.ordinary_plan is None
                or identity.elevated_plan is None
            ):
                raise StateError("Recovery span guest and numeric preparation is unavailable")
            guest = selected._matching_ready_guest(ready, guest_preparation)  # noqa: SLF001
            if guest is None or guest.instance_marker != current.instance_marker or guest_preparation.target is None:
                raise StateError("Recovery span prepared guest does not match its retained hold")
            self._prepared = RecoveryVMPreparedContext(
                guest_preparation.target,
                guest,
                identity.ordinary_plan,
                identity.elevated_plan,
                binding.runtime_selection,
                _ActionCarrier(self, binding.carrier, None),
                self,
            )
            self._revalidate_action(deadline)
            return self._prepared
        except BaseException as control:
            raise control from RecoveryVMSpanControlFact(self)
        finally:
            self._transition.release()

    def _revalidate_action(self, deadline: Deadline) -> None:
        self._require_owner()
        self._fresh_vm(deadline, action=True)
        selected = self._selected
        if selected is None or self._prepared is None or self._closed:
            raise StateError("Recovery span is not prepared")
        selected.require_selected_route(deadline)
        self._require_ready_hold(selected, deadline)

    @staticmethod
    def _require_ready_hold(selected: WSL2OwnedOperation, deadline: Deadline) -> None:
        ready = selected.ready
        if ready is None or not selected._ready_is_durable(ready):  # noqa: SLF001
            raise StateError("Recovery span durable hold is unavailable")
        # Current host custody is only a negative failure screen. It supplies no
        # continuing guest-liveness or queued native-dispatch drain proof.
        evidence = selected.hold.evidence
        local = selected.hold._current_local_snapshot()  # noqa: SLF001
        if deadline.expired:
            raise TimeoutError("Recovery span hold observation exceeded its deadline")
        if (
            local.host_client_status is not HostClientStatus.ACTIVE
            or local.job_assignment is not JobAssignment.ASSIGNED_AT_CREATION
            or local.host_handle_settlement is not HandleSettlement.OPEN
            or local.job_handle_settlement is not HandleSettlement.OPEN
            or evidence.identity != ready.identity
        ):
            raise StateError("Recovery span hold has known unavailable local custody")

    @contextmanager
    def action(self, deadline: Deadline) -> Iterator[RecoveryVMPreparedContext]:
        """Protect one adapter's bounded action against availability release."""
        self._acquire(deadline)
        action = _Action(get_ident(), deadline)
        try:
            if self._closing or self._closed:
                raise StateError("Recovery span action admission is closed")
            self._require_owner(idle=True)
            self._revalidate_action(deadline)
            prepared, selected = self._prepared, self._selected
            assert prepared is not None and selected is not None
            yield replace(prepared, carrier=_ActionCarrier(self, selected.binding.carrier, action))
        except BaseException as control:
            raise control from RecoveryVMSpanControlFact(self)
        finally:
            action.active = False
            self._transition.release()

    def _require_prepared_action(
        self, context: RecoveryVMPreparedContext, owner: OperationOwner, deadline: Deadline | None = None
    ) -> None:
        """Validate private adapter facts against this actual protected action."""
        carrier = context.carrier
        prepared, selected = self._prepared, self._selected
        if (
            owner is not self._owner
            or context._span is not self
            or prepared is None
            or selected is None
            or type(carrier) is not _ActionCarrier
            or carrier._span is not self
            or carrier._carrier is not selected.binding.carrier
            or replace(context, carrier=prepared.carrier) != prepared
        ):
            raise StateError("Recovery context is not this span's prepared action")
        carrier._require_action()
        action = carrier._action
        assert action is not None and action.deadline.expires_at is not None
        budget = action.deadline if deadline is None else deadline
        if type(budget) is not Deadline or budget.expires_at is None or budget.expires_at > action.deadline.expires_at:
            raise ValidationError("Recovery context deadline exceeds its action budget")
        self._revalidate_action(budget)

    def close(self, deadline: Deadline) -> None:
        """Stop span admission and release only settled own availability custody."""
        self._closing = True
        self._acquire(deadline)
        try:
            if self._closed:
                return
            if not self._local_delivery.close(deadline) or not self._owner.close_local_delivery(deadline):
                raise StateError("Recovery span retains unsettled local delivery")
            selected, batch = self._selected, self._batch
            if selected is None or not self._hold_started:
                self._closed = True
                return
            self._require_owner()
            if batch is not None and batch.preparation.requires_owner_retention:
                batch.retry_resolution()
            self._require_owner(idle=True)
            hold = selected.hold
            if hold.registration_uncertain:
                raise StateError("Recovery span hold registration is uncertain")
            evidence = hold.evidence
            never_created = evidence.local.settled and evidence.local.host_client_status is HostClientStatus.NOT_CREATED
            if hold.payload is None and never_created:
                self._closed = True
                return
            rows = self._owner.list_pending_lifecycle_obligations()
            if not never_created and any(row.obligation_id != self._hold_id for row in rows):
                raise StateError("Recovery span retains unsettled operation debt")
            released = hold.release(deadline)
            if not released.local.settled or not (
                never_created
                or released.identity is not None
                and released.guest_anchor_presence is GuestAnchorPresence.ABSENT_CONFIRMED
                and (selected.ready is None or released.identity == selected.ready.identity)
            ):
                raise StateError("Recovery span hold release is uncertain")
            self._closed = True
        except BaseException as control:
            raise control from RecoveryVMSpanControlFact(self)
        finally:
            self._transition.release()
