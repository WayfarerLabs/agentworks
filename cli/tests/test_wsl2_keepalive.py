"""Tests for the WSL2 vm_active / keepalive primitive.

The keepalive spawns ``wsl --distribution NAME -- sleep infinity`` as a
background subprocess so Windows' WSL idle timer doesn't tear the distro
down while agentworks is still using it. These tests pin the subprocess
lifecycle without actually running wsl.exe.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentworks.vms.base import VMProvisioner
from agentworks.vms.provisioners.wsl2 import WSL2Provisioner


def _fake_vm(name: str = "wsltest", tailscale_host: str | None = None) -> Any:
    vm = MagicMock()
    vm.name = name
    vm.platform = "wsl2"
    vm.tailscale_host = tailscale_host
    vm.admin_username = "agentworks"
    return vm


def _running_proc() -> MagicMock:
    """A Popen mock that models a subprocess that's still running on the
    initial fast-fail probe (raises TimeoutExpired) and exits cleanly on the
    final terminate-then-wait."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="wsl", timeout=0.5), None]
    return proc


def test_vm_active_spawns_keepalive_subprocess() -> None:
    """Entering vm_active spawns `wsl --distribution NAME -- sleep infinity`."""
    proc = _running_proc()
    with patch("agentworks.vms.provisioners.wsl2.subprocess.Popen", return_value=proc) as popen:
        with WSL2Provisioner().vm_active(_fake_vm("mydistro")):
            popen.assert_called_once()
            args = popen.call_args[0][0]
            assert args == ["wsl", "--distribution", "mydistro", "--", "sleep", "infinity"]
            kwargs = popen.call_args.kwargs
            # stdin/stdout detached so the subprocess doesn't compete for the user's terminal.
            # stderr is piped so we can read it on a fast-fail.
            assert kwargs["stdin"] == subprocess.DEVNULL
            assert kwargs["stdout"] == subprocess.DEVNULL
            assert kwargs["stderr"] == subprocess.PIPE
    # On clean exit: terminate then wait. No kill needed.
    proc.terminate.assert_called_once()
    # wait is called twice: once for the fast-fail probe, once after terminate.
    assert proc.wait.call_count == 2
    proc.kill.assert_not_called()


def test_vm_active_terminates_on_exception() -> None:
    """If the wrapped block raises, the keepalive is still cleaned up."""
    proc = _running_proc()
    with patch("agentworks.vms.provisioners.wsl2.subprocess.Popen", return_value=proc):
        with pytest.raises(RuntimeError, match="boom"):
            with WSL2Provisioner().vm_active(_fake_vm()):
                raise RuntimeError("boom")
    proc.terminate.assert_called_once()
    proc.wait.assert_called()


def test_vm_active_kills_if_terminate_doesnt_take() -> None:
    """If terminate's wait times out, fall back to kill()."""
    proc = MagicMock(spec=subprocess.Popen)
    # 1: fast-fail probe (still running), 2: post-terminate wait (timeout),
    # 3: post-kill wait (exits).
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="wsl", timeout=0.5),
        subprocess.TimeoutExpired(cmd="wsl", timeout=5),
        None,
    ]
    with patch("agentworks.vms.provisioners.wsl2.subprocess.Popen", return_value=proc):
        with WSL2Provisioner().vm_active(_fake_vm()):
            pass
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


def test_vm_active_fast_fails_if_keepalive_subprocess_dies_immediately() -> None:
    """If wsl.exe exits within 0.5s, that's a failed keepalive -- raise loudly.

    Silent fallthrough would leave callers thinking they're anchored when they
    aren't, then hitting confusing idle-shutdown timeouts mid-operation.
    """
    proc = MagicMock(spec=subprocess.Popen)
    proc.wait.return_value = 1  # rc 1, exited
    # `spec=subprocess.Popen` doesn't auto-expose stderr (it's an instance attr).
    proc.stderr = MagicMock()
    proc.stderr.read.return_value = b"wsl: distro not found\n"
    with patch("agentworks.vms.provisioners.wsl2.subprocess.Popen", return_value=proc):
        with pytest.raises(RuntimeError, match="exited immediately"):
            with WSL2Provisioner().vm_active(_fake_vm("missing-distro")):
                pass


def test_vm_active_waits_for_tailscale_when_host_known() -> None:
    """If vm.tailscale_host is set AND config is provided, wait for SSH."""
    proc = _running_proc()
    fake_target = MagicMock()
    fake_config = MagicMock()
    with (
        patch("agentworks.vms.provisioners.wsl2.subprocess.Popen", return_value=proc),
        patch("agentworks.vms.provisioners.wsl2.admin_exec_target", return_value=fake_target) as build,
        patch("agentworks.vms.provisioners.wsl2.wait_for_reconnect") as wait,
    ):
        vm = _fake_vm(tailscale_host="100.64.0.5")
        with WSL2Provisioner().vm_active(vm, config=fake_config):
            pass
        build.assert_called_once_with(vm, fake_config)
        wait.assert_called_once_with(fake_target)


def test_vm_active_skips_tailscale_wait_when_host_unknown() -> None:
    """Pre-bootstrap: no tailscale_host, no wait, no SSH config build."""
    proc = _running_proc()
    with (
        patch("agentworks.vms.provisioners.wsl2.subprocess.Popen", return_value=proc),
        patch("agentworks.vms.provisioners.wsl2.admin_exec_target") as build,
        patch("agentworks.vms.provisioners.wsl2.wait_for_reconnect") as wait,
    ):
        with WSL2Provisioner().vm_active(_fake_vm(tailscale_host=None), config=MagicMock()):
            pass
        build.assert_not_called()
        wait.assert_not_called()


def test_vm_active_skips_tailscale_wait_when_config_missing() -> None:
    """Even with a known host, no config means we can't build an SSH target."""
    proc = _running_proc()
    with (
        patch("agentworks.vms.provisioners.wsl2.subprocess.Popen", return_value=proc),
        patch("agentworks.vms.provisioners.wsl2.admin_exec_target") as build,
        patch("agentworks.vms.provisioners.wsl2.wait_for_reconnect") as wait,
    ):
        with WSL2Provisioner().vm_active(_fake_vm(tailscale_host="100.64.0.5"), config=None):
            pass
        build.assert_not_called()
        wait.assert_not_called()


def test_base_provisioner_vm_active_is_nullcontext() -> None:
    """Lima/Azure/Proxmox inherit the no-op default. Nothing is spawned."""

    class _Stub(VMProvisioner):
        def create(self, vm_name: str, config: object) -> Any:  # type: ignore[override]
            raise NotImplementedError

        def start(self, vm: Any) -> None:
            raise NotImplementedError

        def stop(self, vm: Any) -> None:
            raise NotImplementedError

        def delete(self, vm: Any) -> None:
            raise NotImplementedError

        def status(self, vm: Any) -> Any:
            raise NotImplementedError

        def admin_exec_target(self, vm: Any, *, config: object | None = None) -> Any:
            raise NotImplementedError

    # Patch Popen at the wsl2 module level; the base default must NOT touch it.
    with patch("agentworks.vms.provisioners.wsl2.subprocess.Popen") as popen:
        with _Stub().vm_active(_fake_vm()):
            pass
        popen.assert_not_called()
