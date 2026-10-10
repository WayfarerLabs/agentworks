"""Proxmox availability custody around one exact activation request."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.db import LifecycleObligationState, VMStatus
from agentworks.errors import StateError
from agentworks.execution._proxmox_activation import ProxmoxActivation, TaskPhase
from agentworks.vms.target_preparation import VMTargetPreparationControlFact, prepare_managed_vm_target_from_platform

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.base import RunContext
    from agentworks.db import VMRow
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import Deadline
    from agentworks.execution.carriers.proxmox import ProxmoxCarrier
    from agentworks.operations import OperationOwner
    from agentworks.plugins.proxmox.platform import ProxmoxPlatform
    from agentworks.vms.target_preparation import VMTargetPreparation


@dataclass
class ProxmoxOwnedNativePlatformAccess:
    """Retain activation and preparation facts without stopping the VM at close."""

    vm: VMRow
    platform: ProxmoxPlatform
    ctx: RunContext
    owner: OperationOwner
    custody: LocalDeliveryCustody
    binding: NativeExecutionBinding | None = None
    preparation: VMTargetPreparation | None = None
    locator: ProviderLocator | None = None
    activation: ProxmoxActivation | None = None
    route_check: Callable[[Deadline], None] | None = None
    _used: bool = field(default=False, init=False, repr=False)

    def observe_power(self, deadline: Deadline) -> VMStatus:
        return self.platform.observe_execution_power(self.vm, self.ctx, deadline=deadline, custody=self.custody)

    def prepare(self, power: VMStatus, deadline: Deadline) -> None:
        if self._used:
            raise StateError("Owned Proxmox native access preparation is single use")
        self._used = True
        locator = self.platform.observe_provider_locator(self.vm, self.ctx, deadline=deadline, custody=self.custody)
        if deadline.expired:
            raise StateError(
                "Native VM locator observation exceeded its deadline", entity_kind="vm", entity_name=self.vm.name
            )
        if type(locator) is not ProviderLocator:
            raise StateError("Native VM route is unavailable", entity_kind="vm", entity_name=self.vm.name)
        self.locator = locator
        self.route_check = self._check_route
        binding = self.platform.resolve_native_execution_binding(
            self.vm, self.ctx, deadline=deadline, config=self.ctx.config
        )
        if deadline.expired:
            raise StateError(
                "Native VM binding resolution exceeded its deadline", entity_kind="vm", entity_name=self.vm.name
            )
        self.binding = binding
        if power is VMStatus.STOPPED:
            _activate_proxmox(self, binding, locator, deadline)
        try:
            preparation = prepare_managed_vm_target_from_platform(
                self.vm,
                self.platform,
                self.ctx,
                locator,
                binding,
                deadline=deadline,
                owner=self.owner,
                provider_custody=self.custody,
            )
        except BaseException as control:
            if isinstance(control.__cause__, VMTargetPreparationControlFact):
                self.preparation = control.__cause__.preparation
            raise
        self.preparation = preparation

    def _check_route(self, deadline: Deadline) -> None:
        """Re-observe the retained provider locator, not the guest boot identity."""
        if deadline.expires_at is None or deadline.expired:
            raise StateError("Proxmox route check requires a fresh finite deadline")
        if not self.custody.close(deadline):
            raise StateError("Proxmox route check retains unsettled local delivery")
        locator = self.platform.observe_provider_locator(self.vm, self.ctx, deadline=deadline, custody=self.custody)
        if deadline.expired or not self.custody.settled or self.locator is None or locator != self.locator:
            raise StateError("Proxmox provider locator changed or is unavailable")

    def settle(self, deadline: Deadline) -> bool:
        activation = self.activation
        if activation is None:
            return True
        row = self.owner.inspect_lifecycle_obligation(activation.obligation_id)
        obligation = activation.obligation
        if row is None and obligation is None:
            # No POST can precede a returned registration handle and durable arm.
            return True
        if (
            obligation is not None
            and row is not None
            and row == obligation._persisted_obligation  # noqa: SLF001
            and row.state is LifecycleObligationState.RESOLVED
        ):
            return True
        activation.reconcile(deadline)
        if activation.obligation is not None and activation.obligation.state is LifecycleObligationState.RESOLVED:
            return True
        if activation.payload.upid is not None:
            _wait_activation(activation, deadline)
            return True
        return False


def _remaining(deadline: Deadline) -> float:
    remaining = deadline.remaining()
    assert remaining is not None
    if remaining <= 0:
        raise StateError("Native Proxmox startup exceeded its deadline")
    return remaining


def _pause(deadline: Deadline) -> None:
    time.sleep(min(0.1, _remaining(deadline)))
    _remaining(deadline)


def _wait_activation(activation: ProxmoxActivation, deadline: Deadline) -> None:
    while True:
        _remaining(deadline)
        try:
            observation = activation.observe(deadline)
        except Exception:
            raise StateError("Native Proxmox activation observation is unavailable") from None
        if observation.request_settled:
            return
        if observation.ha_handoff or observation.phase is not TaskPhase.RUNNING:
            raise StateError("Native Proxmox activation remains unsettled")
        _pause(deadline)


def _guest_info_responded(data: dict[str, object]) -> bool:
    """Validate the required external guest-info shape, without capability claims."""
    result = data.get("result")
    if type(result) is not dict or type(result.get("version")) is not str:
        return False
    commands = result.get("supported_commands")
    return type(commands) is list and all(
        type(command) is dict
        and type(command.get("name")) is str
        and type(command.get("enabled")) is bool
        and ("success-response" not in command or type(command["success-response"]) is bool)
        for command in commands
    )


def _activate_proxmox(
    access: ProxmoxOwnedNativePlatformAccess,
    binding: NativeExecutionBinding,
    locator: ProviderLocator,
    deadline: Deadline,
) -> None:
    carrier = cast("ProxmoxCarrier", binding.carrier)
    wire = carrier._wire  # noqa: SLF001
    access.activation = ProxmoxActivation(
        access.owner,
        access.vm.name,
        wire._connection,
        locator,
        custody=access.custody,  # noqa: SLF001
    )
    try:
        access.activation.start(deadline)
    except Exception:
        raise StateError("Native Proxmox activation request is unavailable") from None
    _wait_activation(access.activation, deadline)
    try:
        power = wire.request_power(timeout=_remaining(deadline), custody=access.custody)
    except Exception:
        raise StateError("Native Proxmox current power is unavailable") from None
    _remaining(deadline)
    if power.get("status") != "running":
        raise StateError("Native Proxmox activation did not establish running power")
    while True:
        access.owner.list_pending_lifecycle_obligations()
        try:
            info = wire.request_guest_info(timeout=_remaining(deadline), custody=access.custody)
        except Exception:
            info = {}
        if not access.custody.settled:
            raise StateError("Native Proxmox guest information retains unsettled local delivery")
        _remaining(deadline)
        access.owner.list_pending_lifecycle_obligations()
        if _guest_info_responded(info):
            _remaining(deadline)
            return
        _pause(deadline)
