"""Buffered-carrier custody routing and owned local constructor-delay evidence."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

from agentworks.errors import StateError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    Deadline,
    Dispatch,
    Failure,
    PreparedInvocation,
)
from agentworks.execution.carriers import wsl2
from agentworks.execution.carriers._subprocess import ProcessResult
from agentworks.execution.carriers.ssh import client
from agentworks.execution.carriers.ssh.connection import SSHConnection


def _carrier(kind: str, tmp_path: Path) -> wsl2.WSL2Carrier | client.SSHCarrier:
    if kind == "wsl":
        return wsl2.WSL2Carrier(wsl2.WSL2Connection("fixture", "user", "wsl.exe"))
    key, trust = tmp_path / "key", tmp_path / "trust"
    key.write_bytes(b"fixture")
    trust.write_bytes(b"fixture")
    return client.SSHCarrier(SSHConnection("fixture.invalid", "user", key, trust))


@pytest.mark.parametrize("kind", ["wsl", "ssh"])
def test_unsettled_custody_refuses_before_another_pump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    custody = LocalDeliveryCustody()
    custody.begin_process()
    monkeypatch.setattr(wsl2 if kind == "wsl" else client, "run_process", lambda *_a, **_k: pytest.fail("replayed"))
    try:
        with pytest.raises(StateError):
            _carrier(kind, tmp_path).execute(
                PreparedInvocation(("/fixture",)), io=CarrierIO(), deadline=Deadline.after(1), custody=custody
            )
    finally:
        custody.close(Deadline.after(1))
    assert custody.settled


@pytest.mark.parametrize("kind", ["wsl", "ssh"])
def test_not_started_pending_custody_keeps_dispatch_distinction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    custody = LocalDeliveryCustody()
    calls = []

    def pending(argv, **kwargs):
        assert kwargs["custody"] is custody
        calls.append(argv)
        custody.begin_process()
        return ProcessResult(False, None, None, CapturedOutput(), CapturedOutput(), Failure.DEADLINE)

    monkeypatch.setattr(wsl2 if kind == "wsl" else client, "run_process", pending)
    try:
        report = _carrier(kind, tmp_path).execute(
            PreparedInvocation(("/fixture",)), io=CarrierIO(), deadline=Deadline.after(1), custody=custody
        )
        assert report.dispatch == (Dispatch.UNKNOWN if kind == "wsl" else Dispatch.NOT_SENT)
        assert report.failure == Failure.DEADLINE and report.completion is None
        assert len(calls) == 1 and not custody.settled
    finally:
        custody.close(Deadline.after(1))
    assert custody.settled


def test_ssh_probe_and_dispatch_share_exact_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custody = LocalDeliveryCustody()
    calls = []

    def observed(argv, **kwargs):
        assert kwargs["custody"] is custody
        calls.append(argv)
        stderr = b"OpenSSH_9.9p1" if argv[-1] == "-V" else b"error"
        return ProcessResult(True, 0, 0, CapturedOutput(b"output"), CapturedOutput(stderr), None)

    monkeypatch.setattr(client, "run_process", observed)
    report = _carrier("ssh", tmp_path).execute(
        PreparedInvocation(("/fixture",)), io=CarrierIO(), deadline=Deadline.after(1), custody=custody
    )
    assert report.dispatch == Dispatch.SENT and len(calls) == 2 and custody.settled


def test_ssh_matching_version_with_pending_cleanup_cannot_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custody = LocalDeliveryCustody()
    calls = []

    def pending(argv, **kwargs):
        assert kwargs["custody"] is custody
        calls.append(argv)
        custody.begin_process()
        return ProcessResult(True, 0, 0, CapturedOutput(), CapturedOutput(b"OpenSSH_9.9p1"), None)

    monkeypatch.setattr(client, "run_process", pending)
    try:
        report = _carrier("ssh", tmp_path).execute(
            PreparedInvocation(("/fixture",)), io=CarrierIO(), deadline=Deadline.after(1), custody=custody
        )
        assert report.dispatch == Dispatch.NOT_SENT and report.failure == Failure.OBSERVATION
        assert len(calls) == 1 and not custody.settled
    finally:
        custody.close(Deadline.after(1))


@pytest.mark.windows
@pytest.mark.parametrize("kind", ["wsl", "ssh"])
def test_actual_delayed_constructor_retained_and_never_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    entered, release = Event(), Event()
    custody = LocalDeliveryCustody()
    children = []
    original = subprocess.Popen

    def delayed(argv, **kwargs):
        entered.set()
        assert release.wait(5)
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", delayed)
    carrier = _carrier(kind, tmp_path)
    try:
        report = carrier.execute(
            PreparedInvocation(("/fixture",)), io=CarrierIO(), deadline=Deadline.after(0.1), custody=custody
        )
        assert entered.is_set() and not children and not custody.settled
        assert report.dispatch == (Dispatch.UNKNOWN if kind == "wsl" else Dispatch.NOT_SENT)
        assert report.completion is None
        with pytest.raises(StateError):
            carrier.execute(
                PreparedInvocation(("/fixture",)), io=CarrierIO(), deadline=Deadline.after(1), custody=custody
            )
    finally:
        release.set()
        custody.close(Deadline.after(2))
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
    assert custody.settled and len(children) == 1
    assert all(pipe is None or pipe.closed for pipe in (children[0].stdin, children[0].stdout, children[0].stderr))
