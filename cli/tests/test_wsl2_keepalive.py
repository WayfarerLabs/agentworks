"""Bounded same-keeper attachment and cleanup using real local pipes."""

from __future__ import annotations

import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pytest

from agentworks.capabilities.vm_platform import VMPlatform, wsl2
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.errors import StateError, UserAbort
from agentworks.schema import AgwModel


def _fake_vm(name: str = "wsltest", tailscale_host: str | None = None) -> Any:
    return SimpleNamespace(name=name, platform_metadata={"distro_name": name}, tailscale_host=tailscale_host)


def _child(code: str):
    return subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )


# The simulated guest writes the same LF bytes as guest sh printf, regardless
# of the workstation Python text stream's newline policy.
_GUEST_ACK = "sys.stdout.buffer.write(b'agentworks-wsl-keeper-ready\\n'); sys.stdout.buffer.flush()"


@pytest.mark.windows
def test_synthetic_guest_acknowledgement_bypasses_windows_text_newline_translation():
    text_process = _child(
        "import sys; sys.stdout.reconfigure(newline='\\r\\n'); print('agentworks-wsl-keeper-ready', flush=True)"
    )
    text_output, _ = text_process.communicate(timeout=5)
    assert text_process.returncode == 0
    assert text_output == b"agentworks-wsl-keeper-ready\r\n"

    binary_process = _child(f"import sys; sys.stdout.reconfigure(newline='\\r\\n'); {_GUEST_ACK}")
    binary_output, _ = binary_process.communicate(timeout=5)
    assert binary_process.returncode == 0
    assert binary_output == b"agentworks-wsl-keeper-ready\n"


@pytest.mark.windows
@pytest.mark.parametrize("delay", [0, 0.12])
def test_entry_waits_for_same_guest_keeper_acknowledgement(monkeypatch, delay):
    original_popen = subprocess.Popen
    process = _child(f"import sys,time; time.sleep({delay}); {_GUEST_ACK}; time.sleep(30)")
    called = []

    def spawn(argv, **kwargs):
        called.append(argv)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    entered = time.monotonic()
    with WSL2Platform("wsl2", {}).vm_active(_fake_vm("distro")):
        assert time.monotonic() - entered >= delay * 0.8
        assert process.poll() is None
    assert process.poll() is not None
    assert process.stdout.closed
    assert called[0][:6] == ["wsl", "--distribution", "distro", "--", "sh", "-c"]
    # Run the actual guest command locally to verify its acknowledgement and
    # exec form instead of pinning the authored shell spelling.
    if sys.platform != "win32":
        actual = original_popen(["sh", "-c", called[0][-1]], stdout=subprocess.PIPE)
        try:
            wsl2._await_keeper_attachment(actual)
        finally:
            wsl2._release_keeper(actual)


@pytest.mark.windows
@pytest.mark.parametrize(
    "code",
    [
        "print('bad-marker', flush=True)",
        "pass",
        "print('agentworks-wsl-keeper', end='', flush=True)",
        "print('agentworks-wsl-keeper-ready-extra', flush=True)",
        "import sys; sys.stdout.buffer.write(b'agentworks-wsl-keeper-ready\\r\\n'); sys.stdout.buffer.flush()",
    ],
)
def test_malformed_or_missing_ack_fails_entry_and_reaps(monkeypatch, code):
    process = _child(code)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: process)
    with pytest.raises(StateError), WSL2Platform("wsl2", {}).vm_active(_fake_vm()):
        pytest.fail("body entered without keeper acknowledgement")
    assert process.poll() is not None
    assert process.stdout.closed


@pytest.mark.windows
def test_timeout_does_not_accept_live_client_as_guest_attachment(monkeypatch):
    process = _child("import time; time.sleep(30)")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(wsl2, "_KEEPER_ENTRY_SECONDS", 0.05)
    start = time.monotonic()
    with pytest.raises(StateError), WSL2Platform("wsl2", {}).vm_active(_fake_vm()):
        pytest.fail("body entered before delayed keeper attachment")
    assert time.monotonic() - start < 2
    assert process.poll() is not None
    assert process.stdout.closed


@pytest.mark.windows
@pytest.mark.parametrize("error", [UserAbort("cancelled"), KeyboardInterrupt()])
def test_cancelled_entry_preserves_cancellation_and_closes_job(monkeypatch, error):
    process = _child("import time; time.sleep(30)")
    if sys.platform != "win32":
        process._handle = 1234
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(wsl2, "_create_kill_on_close_job", lambda: 5678)
    monkeypatch.setattr(wsl2, "_assign_process_to_job", lambda *a: True)
    closed: list[int | None] = []
    monkeypatch.setattr(wsl2, "_close_handle", closed.append)

    def interrupt(*args):
        raise error

    monkeypatch.setattr(wsl2, "_await_keeper_attachment", interrupt)
    with pytest.raises(type(error)) as caught, WSL2Platform("wsl2", {}).vm_active(_fake_vm()):
        pytest.fail("body entered after cancelled attachment")
    assert caught.value is error
    assert process.poll() is not None
    assert process.stdout.closed
    assert closed == [5678]


@pytest.mark.windows
def test_body_failure_is_preserved_and_keeper_is_reaped(monkeypatch):
    process = _child(f"import sys,time; {_GUEST_ACK}; time.sleep(30)")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: process)
    error = UserAbort("cancelled body")
    with pytest.raises(UserAbort) as caught, WSL2Platform("wsl2", {}).vm_active(_fake_vm()):
        raise error
    assert caught.value is error
    assert process.poll() is not None
    assert process.stdout.closed


def test_cleanup_kills_after_bounded_terminate_wait():
    process = MagicMock()
    process.wait.side_effect = [subprocess.TimeoutExpired("keeper", 0.5), 0]
    assert wsl2._release_keeper(process)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
    process.stdout.close.assert_called_once()


def test_cleanup_failure_cannot_skip_pipe_close():
    process = MagicMock()
    process.wait.side_effect = subprocess.TimeoutExpired("keeper", 0.5)
    assert not wsl2._release_keeper(process)
    process.stdout.close.assert_called_once()


def test_base_platform_vm_active_is_nullcontext() -> None:
    """Lima/Azure/Proxmox inherit the no-op default. Nothing is spawned."""

    class _StubConfig(AgwModel):
        name: Literal["stub"]

    class _Stub(VMPlatform):
        name = "stub"
        description = "stub"
        contract_version = 1
        config_model = _StubConfig

        def create(self, request: Any, ctx: Any) -> Any:
            raise NotImplementedError

        def start(self, vm: Any, ctx: Any) -> None:
            raise NotImplementedError

        def stop(self, vm: Any, ctx: Any) -> None:
            raise NotImplementedError

        def delete(self, vm: Any, ctx: Any) -> None:
            raise NotImplementedError

        def status(self, vm: Any, ctx: Any) -> Any:
            raise NotImplementedError

        def display_backend_name(self, vm: Any) -> str:
            raise NotImplementedError

        def native_transport(self, vm: Any, ctx: Any, *, config: Any = None) -> Any:
            raise NotImplementedError

    # Patch Popen at the wsl2 module level; the base default must NOT touch it.
    with patch("agentworks.capabilities.vm_platform.wsl2.subprocess.Popen") as popen:
        with _Stub("stub", {}).vm_active(_fake_vm()):
            pass
        popen.assert_not_called()


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="requires native Win32 job objects")
def test_native_job_close_terminates_its_child() -> None:
    """The keepalive's orphan protection must work with real kernel handles."""
    from agentworks.capabilities.vm_platform.wsl2 import (
        _assign_process_to_job,
        _close_handle,
        _create_kill_on_close_job,
    )

    job = _create_kill_on_close_job()
    assert job is not None
    try:
        with subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as child:
            try:
                assert _assign_process_to_job(job, child._handle)  # type: ignore[attr-defined] # Windows Popen handle
                _close_handle(job)
                job = None
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
    finally:
        _close_handle(job)


@pytest.mark.windows
@pytest.mark.parametrize("assignment", [True, False, None])
def test_job_handle_cleanup_on_success_assignment_failure_and_unavailable(monkeypatch, assignment):
    process = _child(f"import sys,time; {_GUEST_ACK}; time.sleep(30)")
    if sys.platform != "win32":
        process._handle = 1234
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(wsl2, "_create_kill_on_close_job", lambda: None if assignment is None else 5678)
    monkeypatch.setattr(wsl2, "_assign_process_to_job", lambda *a: assignment)
    closed: list[int | None] = []
    monkeypatch.setattr(wsl2, "_close_handle", closed.append)
    with WSL2Platform("wsl2", {}).vm_active(_fake_vm()):
        pass
    assert process.poll() is not None and process.stdout.closed
    assert closed == ([5678] if assignment is True else [5678, None] if assignment is False else [None])


@pytest.mark.windows
def test_vm_stop_cannot_pass_admission_before_delayed_keeper_ack(tmp_path, db, monkeypatch):
    import threading

    from agentworks.db import Database, DesiredRuntimeState
    from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard
    from agentworks.vms.manager import set_vm_desired_runtime_state
    from tests.vms.test_vm_nodes import _GatePlatform, _node, _seed

    marker = tmp_path / "allow-ack"
    process = _child(
        "import pathlib,sys,time; "
        f"p=pathlib.Path({str(marker)!r}); "
        "exec('while not p.exists(): time.sleep(.01)'); "
        f"{_GUEST_ACK}; time.sleep(30)"
    )
    spawned = threading.Event()

    def spawn(*args, **kwargs):
        spawned.set()
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(wsl2, "_KEEPER_ENTRY_SECONDS", 3)
    vm = _seed(db)
    platform = _GatePlatform()
    monkeypatch.setattr(platform, "vm_active", lambda *a, **k: wsl2._keepalive("distro"))
    node, _ = _node(db, platform, vm)
    refused = []

    def stop():
        try:
            assert spawned.wait(2)
            other = Database(db.path)
            try:
                set_vm_desired_runtime_state(other, name=vm.name, state=DesiredRuntimeState.STOPPED)
            except NativeSetupBusyError:
                refused.append(True)
            finally:
                other.close()
        finally:
            marker.write_text("ack permitted")

    thread = threading.Thread(target=stop)
    thread.start()
    try:
        with node.hold_active():
            assert refused == [True]
            assert db.get_vm(vm.name).desired_runtime_state is DesiredRuntimeState.RUNNING
            with native_mutation_guard(db.path, vm.name):
                pass
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert process.poll() is not None and process.stdout.closed
