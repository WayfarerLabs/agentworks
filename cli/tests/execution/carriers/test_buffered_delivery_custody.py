"""Buffered-carrier custody routing and owned local constructor-delay evidence."""

from __future__ import annotations

import subprocess
import sys
import time
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
from agentworks.execution.carriers.ssh.trust import SSHTrustFiles


def _carrier(kind: str, tmp_path: Path) -> wsl2.WSL2Carrier | client.SSHCarrier:
    if kind == "wsl":
        return wsl2.WSL2Carrier(wsl2.WSL2Connection("fixture", "user", "wsl.exe"))
    key, trust = tmp_path / "key", tmp_path / "trust"
    key.write_bytes(b"fixture")
    trust.write_bytes(b"fixture")
    return client.SSHCarrier(SSHConnection("fixture.invalid", "user", key, SSHTrustFiles((trust,))))


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
        return ProcessResult(True, 0, 0, CapturedOutput(), CapturedOutput(b"OpenSSH_9.9p1"), Failure.OBSERVATION)

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
    original_clock = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: original_clock() + (60 if entered.is_set() else 0))

    def delayed(argv, **kwargs):
        entered.set()
        assert release.wait(30)
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", delayed)
    carrier = _carrier(kind, tmp_path)
    try:
        report = carrier.execute(
            PreparedInvocation(("/fixture",)), io=CarrierIO(), deadline=Deadline.after(30), custody=custody
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
        assert custody.close(Deadline.after(2))
    assert custody.settled and len(children) == 1
    assert children[0].returncode is not None
    assert all(pipe is None or pipe.closed for pipe in (children[0].stdin, children[0].stdout, children[0].stderr))


@pytest.mark.windows
@pytest.mark.parametrize("kind", ["wsl", "ssh"])
def test_late_cleanup_does_not_rewrite_unknown_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    entered, release = Event(), Event()
    custody = LocalDeliveryCustody()
    children: list[subprocess.Popen[bytes]] = []
    marker = tmp_path / "client-entered"
    original = subprocess.Popen
    original_clock = time.monotonic
    module = wsl2 if kind == "wsl" else client
    pump = module.run_process
    monkeypatch.setattr(time, "monotonic", lambda: original_clock() + (60 if entered.is_set() else 0))

    def spawn(argv, **kwargs):
        if kind == "ssh" and argv[-1] == "-V":
            child = original([sys.executable, "-c", "import sys; sys.stderr.write('OpenSSH_9.9p1')"], **kwargs)
        else:
            entered.set()
            assert release.wait(30)
            child = original(
                [sys.executable, "-c", f"import pathlib,time; pathlib.Path({str(marker)!r}).touch(); time.sleep(30)"],
                **kwargs,
            )
            children.append(child)
            until = original_clock() + 3
            while not marker.exists() and original_clock() < until:
                time.sleep(0.01)
            assert marker.exists()
            return child
        children.append(child)
        return child

    def observe_after_cleanup(argv, **kwargs):
        result = pump(argv, **kwargs)
        if kind == "ssh" and argv[-1] == "-V":
            return result
        assert not result.started and result.failure is Failure.OBSERVATION
        assert not custody.settled
        release.set()
        # The owner may settle between the pump's immutable observation and
        # carrier reduction. It cannot retroactively prove non-dispatch.
        assert custody.close(Deadline.after(3))
        return result

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(module, "run_process", observe_after_cleanup)
    try:
        report = _carrier(kind, tmp_path).execute(
            PreparedInvocation(("/fixture",)), io=CarrierIO(), deadline=Deadline.after(30), custody=custody
        )
        assert marker.exists() and custody.settled
        assert report.dispatch is Dispatch.UNKNOWN
        assert report.completion is None and report.failure is Failure.OBSERVATION
    finally:
        release.set()
        assert custody.close(Deadline.after(3))
    assert len(children) == (1 if kind == "wsl" else 2)
    assert all(child.returncode is not None for child in children)
    assert all(pipe is None or pipe.closed for child in children for pipe in (child.stdin, child.stdout, child.stderr))
