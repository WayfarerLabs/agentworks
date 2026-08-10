"""Platform-complete Phase A discovers connectivity without replaying secrets."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentworks.db import ProvisioningStatus
from agentworks.vms.initializer import driver


class _TailscaleTransport:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: object) -> SimpleNamespace:
        del kwargs
        self.commands.append(command)
        return SimpleNamespace(stdout="ok\n", returncode=0)


@pytest.mark.parametrize("discovery_failure", [None, KeyboardInterrupt("stop")], ids=("ordinary", "interrupt"))
def test_platform_complete_without_ip_never_selects_secret_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    discovery_failure: BaseException | None,
) -> None:
    secret = "phase-a-fallback-sentinel"
    db = MagicMock()
    logger = MagicMock()
    exec_target = MagicMock()
    if discovery_failure is None:
        exec_target.run.return_value = SimpleNamespace(stdout="100.64.0.8\n")
    else:
        exec_target.run.side_effect = discovery_failure
    config = SimpleNamespace(operator=SimpleNamespace(ssh_private_key="/tmp/test-key"))
    monkeypatch.setattr(driver, "SSHTransport", _TailscaleTransport)
    run_bootstrap = MagicMock(side_effect=AssertionError("secret bootstrap selected"))
    monkeypatch.setattr(driver, "_run_bootstrap_script", run_bootstrap)

    def call() -> _TailscaleTransport:
        return driver._phase_a_bootstrap(
            db,
            config,
            SimpleNamespace(),
            "myvm",
            exec_target,
            "/home/agw",
            "agw",
            "lima--myvm",
            logger,
            tailscale_auth_key=secret,
            script_swap=4,
            bootstrap_complete=True,
            tailscale_ip=None,
        )  # type: ignore[return-value]

    if discovery_failure is None:
        ts_target = call()
        assert ts_target.kwargs["host"] == "100.64.0.8"
        db.update_vm_tailscale.assert_called_once_with("myvm", "100.64.0.8")
    else:
        with pytest.raises(type(discovery_failure)) as caught:
            call()
        assert caught.value is discovery_failure

    run_bootstrap.assert_not_called()
    exec_target.run.assert_called_once_with("sudo tailscale ip -4")
    assert secret not in repr(exec_target.run.call_args_list)


def test_wsl2_helper_result_is_persisted_at_manager_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_key = tmp_path / "id.pub"
    public_key.write_text("ssh-ed25519 AAAA test\n")
    db = MagicMock()
    logger = MagicMock()
    exec_target = MagicMock()
    config = SimpleNamespace(
        operator=SimpleNamespace(
            ssh_public_key=public_key,
            ssh_private_key=tmp_path / "id",
        )
    )
    run_bootstrap = MagicMock(return_value="100.64.0.9")
    monkeypatch.setattr(driver, "run_wsl2_bootstrap", run_bootstrap)

    tailscale_ip = driver._run_bootstrap_script(
        db,
        config,
        SimpleNamespace(),
        "myvm",
        exec_target,
        "agw",
        "wsl2--myvm",
        logger,
        tailscale_auth_key="tskey-test",
        script_swap=0,
    )

    run_bootstrap.assert_called_once_with(
        exec_target,
        admin_username="agw",
        ssh_public_key="ssh-ed25519 AAAA test",
        tailscale_auth_key="tskey-test",
        hostname="wsl2--myvm",
        swap_gib=0,
        progress=logger,
    )
    db.update_vm_tailscale.assert_called_once_with("myvm", "100.64.0.9")
    db.update_vm_provisioning_status.assert_called_once_with("myvm", ProvisioningStatus.COMPLETE)
    assert tailscale_ip == "100.64.0.9"
