"""Passive Lima binding from one observed delivery account and directory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution.binding import NativeExecutionBinding, _EarlyGuestFactsRoute, _IndependentJobAvailability
from agentworks.execution.carriers.lima import LimaCarrier, LimaConnection

if TYPE_CHECKING:
    from agentworks.execution._lima_selection import LimaSelection


def lima_native_binding(selected: LimaSelection) -> NativeExecutionBinding:
    """Root entry obtains early facts; their body uses Lima's actual account."""
    connection = LimaConnection(selected.instance, selected.executable, selected.home, selected.directory)
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
        # The ordinary QEMU/VZ runtime outlives shell delivery. Unknown drivers
        # and WSL2 require their own resource availability proof.
        _independent_availability=(
            _IndependentJobAvailability.NO_IDLE_STOP if selected.vm_type in {"qemu", "vz"} else None
        ),
    )
