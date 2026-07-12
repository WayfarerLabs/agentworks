"""Tests for the WSL2 vm_active / keepalive primitive.

The keepalive spawns ``wsl --distribution NAME -- sleep infinity`` as a
background subprocess so Windows' WSL idle timer doesn't tear the distro
down while agentworks is still using it. These tests pin the subprocess
lifecycle without actually running wsl.exe.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentworks.capabilities.vm_platform import VMPlatform
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform


def _fake_vm(name: str = "wsltest", tailscale_host: str | None = None) -> Any:
    vm = MagicMock()
    vm.name = name
    vm.site = "wsl2"
    # The platform reads the distro name from platform metadata (the
    # backend-side name may differ from vm.name under a system slug).
    vm.platform_metadata = {"distro_name": name}
    vm.tailscale_host = tailscale_host
    vm.admin_username = "agentworks"
    return vm


def _running_proc() -> MagicMock:
    """A Popen mock that models a subprocess that's still running on the
    initial fast-fail probe (raises TimeoutExpired) and exits cleanly on the
    final terminate-then-wait. ``_handle`` is set to a dummy int so the
    job-object assignment path has something to pass to AssignProcessToJobObject.
    ``stderr`` is set to a MagicMock because spec=Popen doesn't auto-expose
    it (it's an instance attribute on Popen, set in __init__) and the
    keepalive's exit path now calls proc.stderr.close() to release the fd."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="wsl", timeout=0.5), None]
    proc._handle = 0x1234  # bypass spec=Popen blocking _handle attribute access
    proc.stderr = MagicMock()
    return proc


@contextmanager
def _job_object_mocks(*, create_returns: int | None = 0xABCD, assign_returns: bool = True):
    """Patch the Win32 job-object helpers so tests stay in-process.

    Yields (create_mock, assign_mock, close_mock) for assertions.
    """
    with (
        patch(
            "agentworks.capabilities.vm_platform.wsl2._create_kill_on_close_job",
            return_value=create_returns,
        ) as create,
        patch(
            "agentworks.capabilities.vm_platform.wsl2._assign_process_to_job",
            return_value=assign_returns,
        ) as assign,
        patch("agentworks.capabilities.vm_platform.wsl2._close_handle") as close,
    ):
        yield create, assign, close


def test_vm_active_spawns_keepalive_subprocess() -> None:
    """Entering vm_active spawns `wsl --distribution NAME -- sleep infinity`."""
    proc = _running_proc()
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc) as popen,
        _job_object_mocks(),
        WSL2Platform("wsl2", {}).vm_active(_fake_vm("mydistro"))
    ):
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
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        _job_object_mocks(),
        pytest.raises(RuntimeError, match="boom"),
        WSL2Platform("wsl2", {}).vm_active(_fake_vm())
    ):
        raise RuntimeError("boom")
    proc.terminate.assert_called_once()
    proc.wait.assert_called()


def test_vm_active_kills_if_terminate_doesnt_take() -> None:
    """If terminate's wait times out, fall back to kill()."""
    proc = MagicMock(spec=subprocess.Popen)
    proc._handle = 0x1234
    proc.stderr = MagicMock()  # spec=Popen hides the instance attr; see _running_proc
    # 1: fast-fail probe (still running), 2: post-terminate wait (timeout),
    # 3: post-kill wait (exits).
    proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="wsl", timeout=0.5),
        subprocess.TimeoutExpired(cmd="wsl", timeout=5),
        None,
    ]
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        _job_object_mocks(),
        WSL2Platform("wsl2", {}).vm_active(_fake_vm())
    ):
        pass
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


def test_vm_active_fast_fails_if_keepalive_subprocess_dies_immediately() -> None:
    """If wsl.exe exits within 0.5s, that's a failed keepalive -- raise loudly.

    Silent fallthrough would leave callers thinking they're anchored when they
    aren't, then hitting confusing idle-shutdown timeouts mid-operation.
    """
    proc = MagicMock(spec=subprocess.Popen)
    proc._handle = 0x1234
    proc.wait.return_value = 1  # rc 1, exited
    # `spec=subprocess.Popen` doesn't auto-expose stderr (it's an instance attr).
    proc.stderr = MagicMock()
    proc.stderr.read.return_value = b"wsl: distro not found\n"
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        _job_object_mocks(),
        pytest.raises(RuntimeError, match="exited immediately"),
        WSL2Platform("wsl2", {}).vm_active(_fake_vm("missing-distro")),
    ):
        pass
    # The fast-fail path closes the stderr PIPE handle before raising, so a
    # caller that retries the keepalive doesn't accumulate one leaked fd per
    # failed attempt.
    proc.stderr.close.assert_called_once()


def test_vm_active_closes_stderr_on_normal_exit() -> None:
    """On the normal exit path the stderr PIPE handle is closed too, so the
    subprocess's fd doesn't outlive the keepalive context."""
    proc = _running_proc()
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        _job_object_mocks(),
        WSL2Platform("wsl2", {}).vm_active(_fake_vm()),
    ):
        pass
    proc.stderr.close.assert_called_once()


def test_vm_active_cleanup_tolerates_already_dead_subprocess() -> None:
    """If the keepalive subprocess died on its own between the fast-fail probe
    and context exit (WSL service reset, distro 'wsl --terminate'd by hand),
    proc.terminate() / proc.kill() raise OSError on POSIX. Cleanup must
    swallow those so a successful command doesn't fail on the way out and
    a caller's exception doesn't get masked.
    """
    proc = MagicMock(spec=subprocess.Popen)
    proc._handle = 0x1234
    proc.stderr = MagicMock()
    # Fast-fail probe sees the process still running; later terminate()
    # races and the process is gone -- POSIX raises ProcessLookupError
    # (an OSError subclass), Windows TerminateProcess raises OSError on
    # bad handle.
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="wsl", timeout=0.5), None]
    proc.terminate.side_effect = ProcessLookupError("no such process")
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        _job_object_mocks(),
        WSL2Platform("wsl2", {}).vm_active(_fake_vm()),
    ):
        pass
    proc.terminate.assert_called_once()
    # Even though terminate raised, the rest of the cleanup ran.
    proc.stderr.close.assert_called_once()


def test_vm_active_waits_for_tailscale_when_host_known() -> None:
    """If vm.tailscale_host is set AND config is provided, wait for SSH."""
    proc = _running_proc()
    fake_target = MagicMock()
    fake_config = MagicMock()
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        patch("agentworks.capabilities.vm_platform.wsl2.transport", return_value=fake_target) as build,
        patch("agentworks.capabilities.vm_platform.wsl2.wait_for_reconnect") as wait,
        _job_object_mocks(),
    ):
        vm = _fake_vm(tailscale_host="100.64.0.5")
        with WSL2Platform("wsl2", {}).vm_active(vm, config=fake_config):
            pass
        build.assert_called_once_with(vm, fake_config)
        wait.assert_called_once_with(fake_target)


def test_vm_active_skips_tailscale_wait_when_host_unknown() -> None:
    """Pre-bootstrap: no tailscale_host, no wait, no SSH config build."""
    proc = _running_proc()
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        patch("agentworks.capabilities.vm_platform.wsl2.transport") as build,
        patch("agentworks.capabilities.vm_platform.wsl2.wait_for_reconnect") as wait,
        _job_object_mocks(),
    ):
        with WSL2Platform("wsl2", {}).vm_active(_fake_vm(tailscale_host=None), config=MagicMock()):
            pass
        build.assert_not_called()
        wait.assert_not_called()


def test_vm_active_skips_tailscale_wait_when_config_missing() -> None:
    """Even with a known host, no config means we can't build an SSH target."""
    proc = _running_proc()
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        patch("agentworks.capabilities.vm_platform.wsl2.transport") as build,
        patch("agentworks.capabilities.vm_platform.wsl2.wait_for_reconnect") as wait,
        _job_object_mocks(),
    ):
        with WSL2Platform("wsl2", {}).vm_active(_fake_vm(tailscale_host="100.64.0.5"), config=None):
            pass
        build.assert_not_called()
        wait.assert_not_called()


def test_vm_active_assigns_subprocess_to_kill_on_close_job() -> None:
    """The keepalive subprocess is bound to a Win32 Job Object on entry and
    the job handle is closed on exit -- this is what kills the orphan if the
    Python process dies in a way that bypasses the finally:."""
    proc = _running_proc()
    proc._handle = 0xDEAD
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        _job_object_mocks(create_returns=0xBEEF) as (create, assign, close),
        WSL2Platform("wsl2", {}).vm_active(_fake_vm())
    ):
        pass
    create.assert_called_once_with()
    assign.assert_called_once_with(0xBEEF, 0xDEAD)
    # Handle closed exactly once on the way out.
    close.assert_called_once_with(0xBEEF)


def test_vm_active_closes_job_handle_on_fast_fail() -> None:
    """Fast-fail path must still release the job handle so we don't leak it."""
    proc = MagicMock(spec=subprocess.Popen)
    proc._handle = 0xDEAD
    proc.wait.return_value = 2
    proc.stderr = MagicMock()
    proc.stderr.read.return_value = b""
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        _job_object_mocks(create_returns=0xBEEF) as (_create, _assign, close),
        pytest.raises(RuntimeError, match="exited immediately"),
        WSL2Platform("wsl2", {}).vm_active(_fake_vm()),
    ):
        pass
    close.assert_called_with(0xBEEF)


def test_vm_active_falls_back_when_job_object_unavailable() -> None:
    """If CreateJobObject fails, proceed without orphan cleanup (best-effort).

    The keepalive still works on the happy path; only the hard-kill orphan
    protection is lost. AssignProcessToJobObject must not be called when the
    job creation itself failed.
    """
    proc = _running_proc()
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        _job_object_mocks(create_returns=None) as (create, assign, close),
        WSL2Platform("wsl2", {}).vm_active(_fake_vm())
    ):
        pass
    create.assert_called_once_with()
    assign.assert_not_called()
    # Cleanup is a no-op when the handle is None (verified by the helper itself),
    # but the finally: still passes None through to _close_handle.
    close.assert_called_with(None)


def test_vm_active_releases_job_when_assignment_fails() -> None:
    """If we managed to create the job but AssignProcessToJobObject failed,
    immediately close the job handle so we don't leak it -- and continue
    without orphan protection."""
    proc = _running_proc()
    proc._handle = 0xDEAD
    with (
        patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen", return_value=proc),
        _job_object_mocks(create_returns=0xBEEF, assign_returns=False) as (create, assign, close),
        WSL2Platform("wsl2", {}).vm_active(_fake_vm()),
    ):
        pass
    create.assert_called_once()
    assign.assert_called_once_with(0xBEEF, 0xDEAD)
    # Closed twice: once when assignment failed (immediate cleanup), once in
    # the finally: with None (which is a safe no-op).
    assert close.call_args_list[0].args == (0xBEEF,)
    assert close.call_args_list[-1].args == (None,)


def test_base_platform_vm_active_is_nullcontext() -> None:
    """Lima/Azure/Proxmox inherit the no-op default. Nothing is spawned."""

    class _Stub(VMPlatform):
        name = "stub"
        description = "stub"

        def create(self, request: Any) -> Any:
            raise NotImplementedError

        def start(self, vm: Any) -> None:
            raise NotImplementedError

        def stop(self, vm: Any) -> None:
            raise NotImplementedError

        def delete(self, vm: Any) -> None:
            raise NotImplementedError

        def status(self, vm: Any) -> Any:
            raise NotImplementedError

        def display_backend_name(self, vm: Any) -> str:
            raise NotImplementedError

    # Patch Popen at the wsl2 module level; the base default must NOT touch it.
    with patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen") as popen:
        with _Stub("stub", {}).vm_active(_fake_vm()):
            pass
        popen.assert_not_called()
