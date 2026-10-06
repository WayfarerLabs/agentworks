"""Private selected WSL2 route, platform hold, and exact VM preparation."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Self

from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import LifecycleObligationState, OperationResourceKind
from agentworks.errors import ValidationError
from agentworks.execution._runtime_prerequisite import RuntimeSelection
from agentworks.execution._vm_guest_identity import VMGuestIdentityObservationState
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution._wsl2_guest_observer import WSL2GuestObserver
from agentworks.execution._wsl2_lifecycle import GuestAnchorPresence, OwnedHostClient, WSL2AnchorEvidence
from agentworks.execution._wsl2_platform_hold import WSL2PlatformHold, decode_hold_payload, locator_digest
from agentworks.execution._wsl2_windows import WindowsWSL2HostClient
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection
from agentworks.operations import OperationOwner
from agentworks.vms.identity import validate_vm_instance_marker
from agentworks.vms.target_preparation import (
    VMTargetPreparation,
    VMTargetPreparationStatus,
    prepare_managed_vm_target_from_platform,
)

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.execution._wsl2_lifecycle import GuestAnchorObserver


class WSL2RouteStatus(StrEnum):
    CURRENT = "current"
    CHANGED = "changed"
    UNCONFIRMED = "unconfirmed"


class WSL2RouteRefusal(Exception):
    """A selected route cannot admit a managed start."""

    def __init__(self, status: WSL2RouteStatus) -> None:
        self.status = status
        super().__init__(f"WSL2 selected route is {status.value}")


class WSL2OwnedOperation:
    """One selected route and caller-retained VM claim shared by private operations."""

    _purpose = "operation"

    def __init__(
        self,
        vm: VMRow,
        platform: WSL2Platform,
        ctx: RunContext,
        locator: ProviderLocator,
        connection: WSL2Connection,
        runtime_selection: RuntimeSelection,
        *,
        owner: OperationOwner,
        config: Config | None = None,
        native: OwnedHostClient | None = None,
        observer: GuestAnchorObserver | None = None,
    ) -> None:
        purpose = self._purpose
        self._require_vm_owner(owner, vm)
        if type(locator) is not ProviderLocator or type(connection) is not WSL2Connection:
            raise ValidationError(f"WSL2 {purpose} requires an exact locator and connection")
        if vm.instance_marker is None:
            raise ValidationError(f"WSL2 {purpose} requires a persisted VM marker")
        if type(runtime_selection) is not RuntimeSelection:
            raise ValidationError(f"WSL2 {purpose} requires an exact runtime selection")
        try:
            selected_locator = ProviderLocator(locator.token)
            selected_connection = WSL2Connection(connection.distribution, connection.user, connection.wsl_executable)
            selected_runtime = RuntimeSelection(runtime_selection.target_os, runtime_selection.explicit_path)
        except AttributeError as error:
            raise ValidationError(f"WSL2 {purpose} requires complete selected route facts") from error
        selected_carrier = WSL2Carrier(selected_connection)
        selected_binding = NativeExecutionBinding(selected_carrier, selected_connection.user, selected_runtime)
        selected_native = WindowsWSL2HostClient() if native is None else native
        selected_observer = WSL2GuestObserver(selected_connection) if observer is None else observer
        self.owner = owner
        self._vm = vm
        self._locator = selected_locator
        self._connection = selected_connection
        self._carrier = selected_carrier
        self._binding = selected_binding
        self._runtime = selected_runtime
        self._platform = platform
        self._ctx = ctx
        self._config = config
        self.hold = WSL2PlatformHold(
            self.owner,
            vm.name,
            selected_locator.token,
            vm.instance_marker,
            selected_connection,
            selected_native,
            selected_observer,
        )
        self.ready: WSL2AnchorEvidence | None = None
        self.preparation: VMTargetPreparation | None = None
        self._used = False
        self._hold_released = False

    @classmethod
    def from_platform(
        cls,
        vm: VMRow,
        platform: WSL2Platform,
        ctx: RunContext,
        *,
        owner: OperationOwner,
        deadline: Deadline,
        config: Config | None = None,
        native: OwnedHostClient | None = None,
        observer: GuestAnchorObserver | None = None,
    ) -> Self | None:
        """Select a copied route under the caller's already-acquired VM claim."""
        purpose = cls._purpose
        cls._require_vm_owner(owner, vm)
        if not isinstance(platform, WSL2Platform) or platform.site_name != vm.site:
            raise ValidationError(f"WSL2 {purpose} requires the VM's selected WSL2 platform")
        validate_vm_instance_marker(vm.instance_marker)
        if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
            raise ValidationError(f"WSL2 {purpose} requires an unexpired finite deadline")
        locator = platform.observe_provider_locator(vm, ctx, deadline=deadline)
        if deadline.expired:
            raise ValidationError(f"WSL2 {purpose} locator observation exceeded the deadline")
        if type(locator) is ProviderLocatorUnavailable:
            return None
        if type(locator) is not ProviderLocator:
            raise ValidationError(f"WSL2 {purpose} requires an exact provider locator")
        try:
            locator = ProviderLocator(locator.token)
        except AttributeError as error:
            raise ValidationError(f"WSL2 {purpose} requires a complete provider locator") from error
        binding = platform.resolve_native_execution_binding(vm, ctx, deadline=deadline, config=config)
        if deadline.expired:
            raise ValidationError(f"WSL2 {purpose} binding resolution exceeded the deadline")
        connection = cls._selected_connection(binding)
        if connection.user != vm.admin_username:
            raise ValidationError(f"WSL2 {purpose} binding does not match the VM")
        try:
            runtime_selection = binding.runtime_selection
        except AttributeError as error:
            raise ValidationError(f"WSL2 {purpose} requires a complete native binding") from error
        return cls(
            vm,
            platform,
            ctx,
            locator,
            connection,
            runtime_selection,
            owner=owner,
            config=config,
            native=native,
            observer=observer,
        )

    @classmethod
    def _require_vm_owner(cls, owner: OperationOwner, vm: VMRow) -> None:
        """Enforce the exact owner and VM scope supplied to private composition."""
        if type(owner) is not OperationOwner:
            raise ValidationError(f"WSL2 {cls._purpose} requires an exact operation owner")
        scope = owner.ownership.scope
        if scope.resource_kind is not OperationResourceKind.VM or scope.resource_name != vm.name:
            raise ValidationError(f"WSL2 {cls._purpose} requires the VM's operation owner")

    @classmethod
    def _selected_connection(cls, binding: NativeExecutionBinding) -> WSL2Connection:
        purpose = cls._purpose
        if type(binding) is not NativeExecutionBinding:
            raise ValidationError(f"WSL2 {purpose} requires a WSL2 native binding")
        try:
            carrier = binding.carrier
            if type(carrier) is not WSL2Carrier:
                raise ValidationError(f"WSL2 {purpose} requires a WSL2 native binding")
            connection = carrier.connection
            if type(connection) is not WSL2Connection:
                raise ValidationError(f"WSL2 {purpose} native binding route is inconsistent")
            selected = WSL2Connection(connection.distribution, connection.user, connection.wsl_executable)
            delivery_account = binding.delivery_account
        except AttributeError as error:
            raise ValidationError(f"WSL2 {purpose} requires a complete native binding") from error
        if delivery_account != selected.user:
            raise ValidationError(f"WSL2 {purpose} native binding route is inconsistent")
        return selected

    def start_and_prepare(self, deadline: Deadline) -> VMGuestIdentity | None:
        """Start once and prepare only under a durable READY claim."""
        if self._used:
            raise ValidationError(f"WSL2 owned {self._purpose} is single use")
        self._used = True
        ready = self.hold.start(deadline)
        self.ready = ready
        if not self._ready_is_durable(ready):
            return None
        self.preparation = prepare_managed_vm_target_from_platform(
            self._vm, self._platform, self._ctx, self._locator, self._binding, deadline=deadline, owner=self.owner
        )
        if self.preparation.status is not VMTargetPreparationStatus.PREPARED:
            return None
        return self._matching_ready_guest(ready, self.preparation)

    def _ready_is_durable(self, ready: WSL2AnchorEvidence) -> bool:
        obligation = self.hold.obligation
        identity = ready.identity
        if obligation is None or identity is None:
            return False
        rows = self.owner.list_lifecycle_obligations()
        for row in rows:
            if (
                row.obligation_id != obligation.obligation_id
                or row.state is not LifecycleObligationState.POSSIBLE_EFFECT
            ):
                continue
            payload = decode_hold_payload(row.payload)
            return (
                payload.guest == identity
                and payload.locator_sha256 == locator_digest(self._locator.token)
                and payload.instance_marker == self._vm.instance_marker
            )
        return False

    @staticmethod
    def _matching_ready_guest(ready: WSL2AnchorEvidence, preparation: VMTargetPreparation) -> VMGuestIdentity | None:
        observed = preparation.guest_result
        guest = observed.observation if observed is not None else None
        identity = (
            guest.identity if guest is not None and guest.state is VMGuestIdentityObservationState.RESOLVED else None
        )
        anchor = ready.identity
        if type(identity) is not VMGuestIdentity or anchor is None or preparation.target is None:
            return None
        if identity.boot_id != anchor.boot_id or identity.init_start_ticks != anchor.init_start_ticks:
            return None
        return identity

    def revalidate_selected_route(self, deadline: Deadline) -> WSL2RouteStatus:
        """Classify fresh route facts; propagate exceptional observations unchanged."""
        locator = self._platform.observe_provider_locator(self._vm, self._ctx, deadline=deadline)
        if deadline.expired:
            return WSL2RouteStatus.UNCONFIRMED
        if type(locator) is not ProviderLocator:
            return WSL2RouteStatus.UNCONFIRMED
        try:
            selected = ProviderLocator(locator.token)
        except (AttributeError, TypeError, ValueError, ValidationError):
            return WSL2RouteStatus.UNCONFIRMED
        if selected != self._locator:
            return WSL2RouteStatus.CHANGED
        binding = self._platform.resolve_native_execution_binding(
            self._vm, self._ctx, deadline=deadline, config=self._config
        )
        if deadline.expired:
            return WSL2RouteStatus.UNCONFIRMED
        try:
            connection = self._selected_connection(binding)
        except (AttributeError, TypeError, ValueError, ValidationError):
            return WSL2RouteStatus.UNCONFIRMED
        if connection != self._connection:
            return WSL2RouteStatus.CHANGED
        try:
            runtime = binding.runtime_selection
        except AttributeError:
            return WSL2RouteStatus.UNCONFIRMED
        if type(runtime) is not RuntimeSelection:
            return WSL2RouteStatus.UNCONFIRMED
        if runtime != self._runtime:
            return WSL2RouteStatus.CHANGED
        confirmation = self._platform.observe_provider_locator(self._vm, self._ctx, deadline=deadline)
        if deadline.expired:
            return WSL2RouteStatus.UNCONFIRMED
        if type(confirmation) is not ProviderLocator:
            return WSL2RouteStatus.UNCONFIRMED
        try:
            confirmed = ProviderLocator(confirmation.token)
        except (AttributeError, TypeError, ValueError, ValidationError):
            return WSL2RouteStatus.UNCONFIRMED
        return WSL2RouteStatus.CURRENT if confirmed == self._locator else WSL2RouteStatus.CHANGED

    def require_selected_route(self, deadline: Deadline) -> None:
        """Refuse admission unless every selected route fact is fresh and equal."""
        status = self.revalidate_selected_route(deadline)
        if status is not WSL2RouteStatus.CURRENT:
            raise WSL2RouteRefusal(status)

    def release_hold_if_settled(self, deadline: Deadline, *, safe: bool) -> bool:
        """Report exact hold release, never aggregate whole-operation resolution.

        The caller supplies permission to attempt hold cleanup. A true result
        leaves the owner active, unsealed, and under the caller's custody.
        """
        if not safe:
            return False
        if self._hold_released:
            return True
        ready = self.ready
        if ready is None or not self._ready_is_durable(ready):
            return False
        released = self.hold.release(deadline)
        if not (
            released.local.settled
            and released.identity == ready.identity
            and released.guest_anchor_presence is GuestAnchorPresence.ABSENT_CONFIRMED
        ):
            return False
        self._hold_released = True
        return True
