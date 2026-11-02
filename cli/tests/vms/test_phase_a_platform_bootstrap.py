"""Complete-or-raise Phase A records and verifies platform bootstrap."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from agentworks.errors import ProvisioningError
from agentworks.execution import trust
from agentworks.vms.initializer import driver
from tests.conftest import ssh_enrolls_on_first_use


class _TailscaleTransport:
    """Stands in for the Tailscale ``SSHTransport``; every instance is recorded
    so a test can see which trust each Phase A transport was bound to."""

    built: ClassVar[list[_TailscaleTransport]] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.commands: list[tuple[str, dict[str, object]]] = []
        self.built.append(self)

    def run(self, command: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append((command, kwargs))
        ssh_enrolls_on_first_use(self.kwargs["trust"])
        return SimpleNamespace(stdout="ok\n", returncode=0)


@pytest.fixture(autouse=True)
def _reset_built_transports() -> None:
    _TailscaleTransport.built.clear()


def test_the_returned_address_is_recorded_in_canonical_form(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key="/tmp/test-key"))
    monkeypatch.setattr(driver, "SSHTransport", _TailscaleTransport)

    ts_target = driver._phase_a_bootstrap(
        db,
        config,  # type: ignore[arg-type]
        "myvm",
        "agw",
        MagicMock(),
        tailnet_address=" 100.64.0.9\n",
    )

    assert ts_target.kwargs["host"] == "100.64.0.9"  # type: ignore[attr-defined]
    db.update_vm_tailscale.assert_called_once_with("myvm", "100.64.0.9")


# -- Enrollment: Phase A's verify connection writes the VM's host key --------


def _phase_a(monkeypatch: pytest.MonkeyPatch, *, transport: type = _TailscaleTransport) -> _TailscaleTransport:
    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key="/tmp/test-key"))
    monkeypatch.setattr(driver, "SSHTransport", transport)
    return driver._phase_a_bootstrap(  # type: ignore[return-value]
        MagicMock(),
        config,  # type: ignore[arg-type]
        "myvm",
        "agw",
        MagicMock(),
        tailnet_address="100.64.0.9",
    )


def test_the_verify_connection_enrolls_and_phase_b_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    returned = _phase_a(monkeypatch)

    verify, phase_b = _TailscaleTransport.built
    assert verify.kwargs["trust"].mode is trust.TrustMode.ENROLL  # type: ignore[attr-defined]
    assert verify.commands
    assert trust.has_record("myvm")
    assert returned is phase_b
    assert phase_b.kwargs["trust"].mode is trust.TrustMode.VM  # type: ignore[attr-defined]
    assert phase_b.kwargs["host"] == verify.kwargs["host"]
    assert phase_b.commands == []


def test_a_verify_connection_that_leaves_no_key_fails_create(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoKeyWritten(_TailscaleTransport):
        def run(self, command: str, **kwargs: object) -> SimpleNamespace:
            self.commands.append((command, kwargs))
            return SimpleNamespace(stdout="ok\n", returncode=0)

    with pytest.raises(ProvisioningError):
        _phase_a(monkeypatch, transport=_NoKeyWritten)

    assert not trust.has_record("myvm")
