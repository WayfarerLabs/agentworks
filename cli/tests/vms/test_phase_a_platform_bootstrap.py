"""Platform-complete Phase A discovers connectivity without replaying secrets."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
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
            tailscale_ctx=RunContext(),
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
