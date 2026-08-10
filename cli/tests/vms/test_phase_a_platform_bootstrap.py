"""Complete-or-raise Phase A records and verifies platform bootstrap."""

from __future__ import annotations

import inspect
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.vm_platform import ProvisionRequest, ProvisionResult, tailscale_join
from agentworks.db import ProvisioningStatus
from agentworks.vms.initializer import driver


class _TailscaleTransport:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.commands: list[tuple[str, dict[str, object]]] = []

    def run(self, command: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append((command, kwargs))
        return SimpleNamespace(stdout="ok\n", returncode=0)


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
        "exec_target",
        "admin_username",
        "logger",
        "tailscale_ip",
    }
    driver_source = inspect.getsource(driver)
    assert "run_wsl2_bootstrap" not in driver_source
    assert "generate_bootstrap_script" not in driver_source


@pytest.mark.parametrize("discovery_failure", [None, KeyboardInterrupt("stop")], ids=("success", "interrupt"))
def test_missing_platform_ip_runs_only_ip_discovery(
    monkeypatch: pytest.MonkeyPatch,
    discovery_failure: BaseException | None,
) -> None:
    db = MagicMock()
    logger = MagicMock()
    exec_target = MagicMock()
    if discovery_failure is None:
        exec_target.run.return_value = SimpleNamespace(stdout="100.64.0.8\n")
    else:
        exec_target.run.side_effect = discovery_failure
    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key="/tmp/test-key"))
    monkeypatch.setattr(driver, "SSHTransport", _TailscaleTransport)

    def call() -> _TailscaleTransport:
        return driver._phase_a_bootstrap(
            db,
            config,
            "myvm",
            exec_target,
            "agw",
            logger,
            tailscale_ip=None,
        )  # type: ignore[return-value]

    if discovery_failure is None:
        ts_target = call()
        assert ts_target.kwargs["host"] == "100.64.0.8"
        assert ts_target.commands == [("echo ok", {"timeout": 15})]
        db.update_vm_tailscale.assert_called_once_with("myvm", "100.64.0.8")
        db.update_vm_provisioning_status.assert_any_call("myvm", ProvisioningStatus.COMPLETE)
    else:
        with pytest.raises(type(discovery_failure)) as caught:
            call()
        assert caught.value is discovery_failure

    exec_target.run.assert_called_once_with("tailscale ip -4", sudo=True)


def test_returned_platform_ip_skips_provisioning_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    logger = MagicMock()
    exec_target = MagicMock()
    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key="/tmp/test-key"))
    monkeypatch.setattr(driver, "SSHTransport", _TailscaleTransport)

    ts_target = driver._phase_a_bootstrap(
        db,
        config,
        "myvm",
        exec_target,
        "agw",
        logger,
        tailscale_ip="100.64.0.9",
    )

    exec_target.run.assert_not_called()
    assert ts_target.kwargs["host"] == "100.64.0.9"  # type: ignore[attr-defined]
    db.update_vm_tailscale.assert_called_once_with("myvm", "100.64.0.9")
