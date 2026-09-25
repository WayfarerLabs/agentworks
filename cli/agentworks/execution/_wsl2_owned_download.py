"""Private ordinary WSL2 hold, target, and DOWNLOAD composition."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.capabilities.vm_platform.base import ProviderLocator, ProviderLocatorUnavailable
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
from agentworks.execution._file_download import FileDownloadOutcome, FileDownloadStatus
from agentworks.execution._file_operation import FileOperation
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
    from agentworks.db.operations import OperationRepository
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution._wsl2_lifecycle import GuestAnchorObserver
    from agentworks.execution.carrier import ByteSink


class WSL2DownloadStatus(StrEnum):
    COMPLETE = "complete"
    REFUSED = "refused"
    RETAINED = "retained"


class WSL2OwnedDownload:
    """One caller-retained private operation; escaping control flow keeps its claim."""

    def __init__(
        self,
        repository: OperationRepository,
        vm: VMRow,
        platform: WSL2Platform,
        ctx: RunContext,
        locator: ProviderLocator,
        connection: WSL2Connection,
        runtime_selection: RuntimeSelection,
        *,
        config: Config | None = None,
        native: OwnedHostClient | None = None,
        observer: GuestAnchorObserver | None = None,
    ) -> None:
        if type(locator) is not ProviderLocator or type(connection) is not WSL2Connection:
            raise ValidationError("WSL2 download requires an exact locator and connection")
        if vm.instance_marker is None:
            raise ValidationError("WSL2 download requires a persisted VM marker")
        selected_carrier = WSL2Carrier(connection)
        selected_native = WindowsWSL2HostClient() if native is None else native
        selected_observer = WSL2GuestObserver(connection) if observer is None else observer
        self.owner = OperationOwner.acquire(
            repository, OperationScope(OperationResourceKind.VM, vm.name), "wsl2-download"
        )
        self._repository = repository
        self._vm = vm
        self._locator = locator
        self._connection = connection
        self._carrier = selected_carrier
        self._runtime = runtime_selection
        self._platform = platform
        self._ctx = ctx
        self._config = config
        try:
            self.hold = WSL2PlatformHold(
                self.owner,
                vm.name,
                locator.token,
                vm.instance_marker,
                connection,
                selected_native,
                selected_observer,
            )
        except BaseException as construction_error:
            # No hold activation or obligation can occur during construction.
            try:
                self.owner.close()
            except BaseException as close_error:
                raise close_error from construction_error
            raise
        self.preparation: VMTargetPreparation | None = None
        self.file_operation: FileOperation | None = None
        self.outcome: FileDownloadOutcome | None = None
        self._used = False

    @classmethod
    def from_platform(
        cls,
        repository: OperationRepository,
        vm: VMRow,
        platform: WSL2Platform,
        ctx: RunContext,
        *,
        deadline: Deadline,
        config: Config | None = None,
        native: OwnedHostClient | None = None,
        observer: GuestAnchorObserver | None = None,
    ) -> WSL2OwnedDownload | None:
        """Resolve the selected WSL2 registration and route before ownership.

        An unavailable initial locator refuses with no claim. Invalid selected
        facts raise before acquiring a claim. This is a private composition path.
        """
        if not isinstance(platform, WSL2Platform) or platform.site_name != vm.site:
            raise ValidationError("WSL2 download requires the VM's selected WSL2 platform")
        validate_vm_instance_marker(vm.instance_marker)
        if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
            raise ValidationError("WSL2 download requires an unexpired finite deadline")
        locator = platform.observe_provider_locator(vm, ctx, deadline=deadline)
        if deadline.expired:
            raise ValidationError("WSL2 download locator observation exceeded the deadline")
        if type(locator) is ProviderLocatorUnavailable:
            return None
        if type(locator) is not ProviderLocator:
            raise ValidationError("WSL2 download requires an exact provider locator")
        binding = platform.resolve_native_execution_binding(vm, ctx, deadline=deadline, config=config)
        if deadline.expired:
            raise ValidationError("WSL2 download binding resolution exceeded the deadline")
        connection = cls._selected_connection(binding)
        if connection.user != vm.admin_username:
            raise ValidationError("WSL2 download binding does not match the VM")
        subject = cls(
            repository,
            vm,
            platform,
            ctx,
            locator,
            connection,
            binding.runtime_selection,
            config=config,
            native=native,
            observer=observer,
        )
        return subject

    @staticmethod
    def _selected_connection(binding: NativeExecutionBinding) -> WSL2Connection:
        if type(binding) is not NativeExecutionBinding or type(binding.carrier) is not WSL2Carrier:
            raise ValidationError("WSL2 download requires a WSL2 native binding")
        connection = binding.carrier.connection
        if type(connection) is not WSL2Connection or binding.delivery_account != connection.user:
            raise ValidationError("WSL2 download native binding route is inconsistent")
        return WSL2Connection(connection.distribution, connection.user, connection.wsl_executable)

    def download(
        self,
        *,
        trusted_root_path: str,
        relative_path: str,
        sink: ByteSink,
        max_bytes: int,
        plan: IdentityPlan,
        deadline: Deadline,
    ) -> WSL2DownloadStatus:
        """Run once; only typed settled obligations permit whole-owner release."""
        if self._used:
            raise ValidationError("WSL2 owned download is single use")
        self._used = True
        ready = self.hold.start(deadline)
        if not self._ready_is_durable(ready):
            return WSL2DownloadStatus.RETAINED

        selected = prepare_managed_vm_target_from_platform(
            self._vm,
            self._platform,
            self._ctx,
            deadline=deadline,
            owner=self.owner,
            config=self._config,
            held_locator=self._locator,
        )
        self.preparation = selected.preparation
        if selected.binding is not None:
            try:
                selected_connection = self._selected_connection(selected.binding)
            except ValidationError:
                return self._release_if_settled(ready, deadline, safe=True)
            if selected_connection != self._connection or selected.binding.runtime_selection != self._runtime:
                return self._release_if_settled(ready, deadline, safe=True)
        if self.preparation.status is not VMTargetPreparationStatus.PREPARED:
            return self._release_if_settled(ready, deadline, safe=not self.preparation.requires_owner_retention)
        if not self._same_ready_epoch(ready, self.preparation):
            return self._release_if_settled(ready, deadline, safe=True)

        target = self.preparation.target
        assert target is not None
        self.file_operation = FileOperation(self.owner, target)
        self.outcome = self.file_operation.download(
            self._carrier,
            trusted_root_path=trusted_root_path,
            relative_path=relative_path,
            sink=sink,
            max_bytes=max_bytes,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime,
        )
        file_settled = (
            not self.outcome.requires_owner_retention
            and not self.file_operation.active_downloads
            and not self.file_operation.unfinished_downloads
        )
        return self._release_if_settled(ready, deadline, safe=file_settled)

    def _ready_is_durable(self, ready: WSL2AnchorEvidence) -> bool:
        obligation = self.hold.obligation
        identity = ready.identity
        if obligation is None or identity is None:
            return False
        rows = self._repository.list_lifecycle_obligations(self.owner.ownership)
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

    def _same_ready_epoch(self, ready: WSL2AnchorEvidence, preparation: VMTargetPreparation) -> bool:
        observed = preparation.guest_result
        guest = observed.observation if observed is not None else None
        identity = (
            guest.identity if guest is not None and guest.state is VMGuestIdentityObservationState.RESOLVED else None
        )
        anchor = ready.identity
        if type(identity) is not VMGuestIdentity or anchor is None or preparation.target is None:
            return False
        return identity.boot_id == anchor.boot_id and identity.init_start_ticks == anchor.init_start_ticks

    def _release_if_settled(self, ready: WSL2AnchorEvidence, deadline: Deadline, *, safe: bool) -> WSL2DownloadStatus:
        """Release the exact hold; retain the claim unless every obligation settled."""
        if not safe:
            return WSL2DownloadStatus.RETAINED
        released = self.hold.release(deadline)
        if not (
            released.local.settled
            and released.identity == ready.identity
            and released.guest_anchor_presence is GuestAnchorPresence.ABSENT_CONFIRMED
        ):
            return WSL2DownloadStatus.RETAINED
        rows = self._repository.list_lifecycle_obligations(self.owner.ownership)
        if not rows or any(row.state is not LifecycleObligationState.RESOLVED for row in rows):
            return WSL2DownloadStatus.RETAINED
        self.owner.seal_lifecycle_obligations()
        self.owner.record_effects_resolved()
        self.owner.close()
        if self.outcome is None or self.outcome.status is not FileDownloadStatus.COMPLETE:
            return WSL2DownloadStatus.REFUSED
        return WSL2DownloadStatus.COMPLETE
