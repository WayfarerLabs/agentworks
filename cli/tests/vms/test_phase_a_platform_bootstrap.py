"""Complete-or-raise Phase A records and verifies platform bootstrap."""

from __future__ import annotations

import inspect
from dataclasses import fields
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.vm_platform import ProvisionRequest, ProvisionResult, tailscale_join
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


def test_fallback_era_contract_shapes_are_structurally_absent() -> None:
    request_fields = {item.name: item for item in fields(ProvisionRequest)}
    result_fields = {item.name for item in fields(ProvisionResult)}

    assert request_fields["tailscale_auth_key"].type == "str"
    assert "progress" in request_fields
    assert "bootstrap_complete" not in result_fields
    assert not hasattr(tailscale_join, "BootstrapCompletion")

    phase_a_parameters = set(inspect.signature(driver._phase_a_bootstrap).parameters)
    assert phase_a_parameters == {
        "db",
        "config",
        "vm_name",
        "admin_username",
        "logger",
        "tailnet_address",
    }
    driver_source = inspect.getsource(driver)
    assert "run_wsl2_bootstrap" not in driver_source
    assert "generate_bootstrap_script" not in driver_source


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


@pytest.mark.parametrize("address", ["", "not-an-address", "100.64.0.9 rm -rf"])
def test_an_address_core_cannot_parse_fails_before_anything_is_recorded_or_connected(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    db = MagicMock()
    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key="/tmp/test-key"))
    monkeypatch.setattr(driver, "SSHTransport", _TailscaleTransport)

    with pytest.raises(ProvisioningError, match="not an IP address"):
        driver._phase_a_bootstrap(db, config, "myvm", "agw", MagicMock(), tailnet_address=address)  # type: ignore[arg-type]

    db.update_vm_tailscale.assert_not_called()
    assert _TailscaleTransport.built == []


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
