"""Passive Lima binding from one observed delivery account and directory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.binding import NativeExecutionBinding, _EarlyGuestFactsRoute, _IndependentJobAvailability
from agentworks.execution.carriers.lima import LimaCarrier, LimaConnection

if TYPE_CHECKING:
    from agentworks.execution._lima_selection import LimaNativeSelection


def lima_independent_availability(vm_type: str) -> _IndependentJobAvailability | None:
    """QEMU/VZ runtime persists across shell delivery; other drivers need proof."""
    return _IndependentJobAvailability.NO_IDLE_STOP if vm_type in {"qemu", "vz"} else None


def lima_native_binding(selected: LimaNativeSelection) -> NativeExecutionBinding:
    """Root entry obtains early facts; their body uses Lima's actual account."""
    provider = selected.provider
    connection = LimaConnection(provider.instance, provider.executable, provider.home, provider.directory)
    carrier = LimaCarrier(connection)
    root_entry = IdentityPlan(
        IdentityExpectation(0, 0, (0,)),
        IdentityMode.DIRECT if selected.user == "root" else IdentityMode.SUDO_ROOT,
    )
    return NativeExecutionBinding(
        carrier,
        selected.user,
        RuntimeSelection(RuntimeTargetOS.LINUX),
        _early_guest_facts_route=_EarlyGuestFactsRoute(carrier, root_entry, selected.user),
        _new_managed_delivery=lambda: LimaCarrier(connection),
        _independent_availability=lima_independent_availability(provider.vm_type),
    )
