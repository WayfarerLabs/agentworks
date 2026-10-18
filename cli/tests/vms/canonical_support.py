"""Synthetic SSH delivery into the existing packed guest/account/body fixture.

This proves core route composition, not SSH credentials or native guest admission.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from agentworks.execution.carriers.ssh import SSHCarrier, SSHConnection, SSHSettings
from agentworks.execution.carriers.wsl2 import WSL2Carrier, WSL2Connection
from tests.vms.test_native_operation import _RouteCarrier

if TYPE_CHECKING:
    import pytest

    from agentworks.config import Config
    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import CarrierIO, CarrierReport, Deadline, PreparedInvocation

ADDRESS = "100.64.0.7"


def canonical_config(tmp_path: Path) -> Config:
    settings = SSHSettings(tmp_path / "trust", tmp_path / "identity")
    return cast("Config", SimpleNamespace(operator=SimpleNamespace(ssh=settings)))


def install_canonical_delivery(monkeypatch: pytest.MonkeyPatch, route: _RouteCarrier) -> list[SSHConnection]:
    connections: list[SSHConnection] = []

    def execute(
        carrier: SSHCarrier,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody,
    ) -> CarrierReport:
        connection = carrier._connection
        connections.append(connection)
        assert connection.host == ADDRESS and connection.user == "admin" and connection.host_key_alias is None
        user = "root" if route.guest.calls == 0 else "admin"
        selected = WSL2Carrier(WSL2Connection("Ubuntu", user, "wsl.exe"))
        return route.execute(selected, invocation, io=io, deadline=deadline, custody=custody)

    monkeypatch.setattr(SSHCarrier, "execute", execute)
    return connections
