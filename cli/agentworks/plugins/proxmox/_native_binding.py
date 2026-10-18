"""Native QGA delivery from one already-selected verified API connection."""

from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.binding import NativeExecutionBinding, _IndependentJobAvailability
from agentworks.execution.carriers._proxmox_helper_delivery import ProxmoxHelperDelivery
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection


def proxmox_native_binding(connection: ProxmoxConnection) -> NativeExecutionBinding:
    """Share exact connection facts across inline, managed and helper deliveries."""
    return NativeExecutionBinding(
        ProxmoxCarrier(connection),
        "root",
        RuntimeSelection(RuntimeTargetOS.LINUX),
        _new_managed_delivery=lambda: ProxmoxCarrier(connection),
        _independent_availability=_IndependentJobAvailability.NO_IDLE_STOP,
        _new_helper_delivery=lambda expectation: ProxmoxHelperDelivery(connection, expectation),
    )
