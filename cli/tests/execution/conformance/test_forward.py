"""The blocking forward: its exit contract against a stand-in client, and a real one against sshd."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks import output
from agentworks.execution import forwarding, ssh_process
from agentworks.execution.errors import ClientUnsupported, TrustMismatch
from agentworks.execution.requests import Forward

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.execution.conformance.lanes import SSHDLane


def stand_in(script: str) -> list[str]:
    return [sys.executable, "-c", script]


@pytest.fixture
def interruptible() -> Iterator[None]:
    """Make SIGINT raise ``KeyboardInterrupt`` even when the runner started us with it ignored.

    A process started as a background job inherits SIGINT as ignored, and Python then installs
    no handler, so a test that raises the signal to model Ctrl-C would wait forever.
    """
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def test_a_client_that_exits_zero_is_status_zero(tmp_path: Path) -> None:
    assert forwarding.run_forward(stand_in("pass"), vm_name="box", log=tmp_path / "log") == 0


def test_a_failed_forward_returns_the_status_and_reports_the_log_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "log"
    log.write_text("\n".join(f"line {n}" for n in range(30)) + "\nbind [127.0.0.1]:80: Address already in use\n")
    shown: list[str] = []
    monkeypatch.setattr(output, "warn", shown.append)
    monkeypatch.setattr(output, "detail", shown.append)

    status = forwarding.run_forward(stand_in("import sys; sys.exit(255)"), vm_name="box", log=log)

    assert status == 255
    assert len(shown) == 1 + ssh_process.CLIENT_LOG_TAIL_LINES
    assert shown[-1].endswith("Address already in use")
    assert "line 0" not in shown


def test_a_host_key_failure_in_the_log_is_a_trust_mismatch(tmp_path: Path) -> None:
    log = tmp_path / "log"
    log.write_text("Host key verification failed.\n")

    with pytest.raises(TrustMismatch):
        forwarding.run_forward(stand_in("import sys; sys.exit(255)"), vm_name="box", log=log)


def test_with_no_log_nothing_is_read_and_the_status_is_returned(tmp_path: Path) -> None:
    assert forwarding.run_forward(stand_in("import sys; sys.exit(3)"), vm_name="box", log=None) == 3


@pytest.mark.skipif(sys.platform == "win32", reason="signal statuses are POSIX")
def test_a_client_killed_by_a_signal_is_128_plus_the_signal(tmp_path: Path) -> None:
    program = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"

    status = forwarding.run_forward(stand_in(program), vm_name="box", log=tmp_path / "log")

    assert status == 128 + signal.SIGKILL


def test_a_client_that_cannot_start_is_a_typed_refusal() -> None:
    with pytest.raises(ClientUnsupported):
        forwarding.run_forward(["/nonexistent/agw-ssh"], vm_name="box", log=None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
def test_an_interrupt_ends_the_client_before_propagating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruptible: None
) -> None:
    marker = tmp_path / "started"
    script = f"import pathlib, time; pathlib.Path({str(marker)!r}).touch(); time.sleep(60)"
    spawned: list[int] = []

    class Recording(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[call-overload]
            spawned.append(self.pid)

    def interrupt_once_started() -> None:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(subprocess, "Popen", Recording)
    threading.Thread(target=interrupt_once_started, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        forwarding.run_forward(stand_in(script), vm_name="box", log=None)

    assert time.monotonic() - started < 10
    with pytest.raises(ProcessLookupError):
        os.kill(spawned[0], 0)  # reaped: the client did not outlive the forward


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
def test_a_real_forward_carries_a_connection_until_interrupted(sshd_lane: SSHDLane, interruptible: None) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        local_port = probe.getsockname()[1]
    banners: list[bytes] = []

    def connect_then_interrupt() -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=2) as connection:
                    banners.append(connection.recv(64))
                    break
            except OSError:
                time.sleep(0.1)
        signal.raise_signal(signal.SIGINT)

    threading.Thread(target=connect_then_interrupt, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        sshd_lane.access.forward([Forward(local_port, sshd_lane.server.port, "127.0.0.1")])

    assert banners
    assert banners[0].startswith(b"SSH-2.0-")


def test_a_forward_that_cannot_bind_exits_non_zero_instead_of_hanging(sshd_lane: SSHDLane) -> None:
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]

        status = sshd_lane.access.forward([Forward(port, sshd_lane.server.port, "127.0.0.1")])

    assert status != 0
