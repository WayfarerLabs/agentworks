"""Private passive WSL binding shared by platform and owned composition."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.binding import NativeExecutionBinding, _EarlyGuestFactsRoute
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection

if TYPE_CHECKING:
    from agentworks.execution._runtime_prerequisite import RuntimeSelection


def _build_wsl2_native_binding(
    connection: WSL2Connection, runtime_selection: RuntimeSelection
) -> NativeExecutionBinding:
    """Retain ordinary delivery while binding one initial root-to-named probe."""
    root_connection = WSL2Connection(connection.distribution, "root", connection.wsl_executable)
    early = _EarlyGuestFactsRoute(
        WSL2Carrier(root_connection),
        IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT),
        connection.user,
    )
    return NativeExecutionBinding(WSL2Carrier(connection), connection.user, runtime_selection, early)
