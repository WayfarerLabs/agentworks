"""Caller-held local delivery settlement, without remote cancellation claims."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution import _process as core
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import CarrierIO, Deadline, Dispatch, Failure, PreparedInvocation
from agentworks.execution.carriers._subprocess import run_process
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, _ProxmoxWire, _WireFailure
from tests.execution.test_proxmox import connection

pytestmark = pytest.mark.windows


def test_empty_custody_is_passive_and_close_is_finite() -> None:
    custody = LocalDeliveryCustody()
    assert custody.settled
    assert custody.close(Deadline.after(0))
    with pytest.raises(ValidationError):
        custody.close(Deadline(None))
    first = custody.begin_process()
    assert not custody.settled
    with pytest.raises(StateError):
        custody.begin_process()
    assert custody.close(Deadline.after(1))
    assert custody.begin_process() is not first
    assert custody.close(Deadline.after(1))


def test_expired_host_attempt_does_not_strand_inert_owner() -> None:
    custody = LocalDeliveryCustody()
    result = run_process(
        [sys.executable, "-c", "raise AssertionError"], io=CarrierIO(), deadline=Deadline.after(0), custody=custody
    )
    assert not result.started
    assert result.failure == Failure.DEADLINE
    assert custody.settled


def test_delayed_real_constructor_returns_pending_and_retains_same_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen
    entered = threading.Event()
    release = threading.Event()
    children: list[subprocess.Popen[bytes]] = []

    def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        entered.set()
        assert release.wait(5)
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    custody = LocalDeliveryCustody()
    started = time.monotonic()
    try:
        result = run_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            io=CarrierIO(),
            deadline=Deadline.after(0.05),
            custody=custody,
        )
        assert entered.is_set()
        assert time.monotonic() - started < 2
        assert result.failure == Failure.OBSERVATION
        assert not result.started  # Not proof of non-dispatch: construction is admitted.
        assert not custody.settled
        with pytest.raises(StateError):
            custody.begin_process()
        assert not custody.close(Deadline.after(0))
    finally:
        release.set()
        assert custody.close(Deadline.after(3))
    assert len(children) == 1
    assert children[0].returncode is not None
    assert custody.settled


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_host_pump_preserves_exact_control_and_settles(
    monkeypatch: pytest.MonkeyPatch, control_type: type[BaseException]
) -> None:
    control = control_type()

    def interrupt(*args: Any, **kwargs: Any) -> Any:
        raise control

    monkeypatch.setattr(core, "_pump_owned_pipes", interrupt)
    custody = LocalDeliveryCustody()
    try:
        with pytest.raises(control_type) as caught:
            run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                io=CarrierIO(),
                deadline=Deadline(None),
                custody=custody,
            )
        assert caught.value is control
        assert custody.settled
    finally:
        assert custody.close(Deadline.after(3))


@pytest.mark.parametrize("natural_exit", [False, True])
def test_failed_cleanup_retains_exact_status_for_explicit_retry_or_natural_exit(
    monkeypatch: pytest.MonkeyPatch, natural_exit: bool
) -> None:
    cleanup = core._cleanup
    statuses: list[core._ProcessStatus] = []

    def fail_first(status: core._ProcessStatus) -> bool:
        statuses.append(status)
        return False if len(statuses) == 1 else cleanup(status)

    monkeypatch.setattr(core, "_cleanup", fail_first)
    custody = LocalDeliveryCustody()
    seconds = 0.3 if natural_exit else 30
    try:
        result = run_process(
            [sys.executable, "-c", f"import time; time.sleep({seconds})"],
            io=CarrierIO(),
            deadline=Deadline.after(0.05),
            custody=custody,
        )
        assert result.failure == Failure.OBSERVATION
        assert not custody.settled
        assert len(statuses) == 1
        with pytest.raises(StateError):
            custody.begin_process()
        if natural_exit:
            deadline = Deadline.after(3)
            while not custody.settled and not deadline.expired:
                time.sleep(0.01)
            assert custody.settled
        else:
            assert custody.close(Deadline.after(2))
        assert len(statuses) == 2
        assert statuses[0] is statuses[1]
    finally:
        assert custody.close(Deadline.after(3))


@pytest.mark.skipif(os.name == "nt", reason="exclusive waitpid loss is POSIX-specific")
def test_lost_native_ownership_cannot_be_replaced_or_signaled(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen
    signals: list[tuple[int, int]] = []

    def externally_reap(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(*args, **kwargs)
        os.waitpid(child.pid, 0)
        return child

    monkeypatch.setattr(subprocess, "Popen", externally_reap)
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    custody = LocalDeliveryCustody()
    result = run_process([sys.executable, "-c", "pass"], io=CarrierIO(), deadline=Deadline.after(3), custody=custody)
    assert result.failure == Failure.OBSERVATION
    assert not custody.settled
    assert not custody.close(Deadline.after(1))
    assert not custody.close(Deadline.after(1))
    with pytest.raises(StateError):
        custody.begin_process()
    assert signals == []


def test_pending_proxmox_post_is_unknown_and_forbids_following_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen
    release = threading.Event()
    calls: list[object] = []

    def delayed(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        calls.append(args)
        assert release.wait(5)
        return original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", delayed)
    custody = LocalDeliveryCustody()
    carrier = ProxmoxCarrier(connection())
    invocation = PreparedInvocation(("/bin/true",))
    try:
        report = carrier.execute(invocation, io=CarrierIO(), deadline=Deadline.after(0.05), custody=custody)
        assert report.dispatch == Dispatch.UNKNOWN
        assert not custody.settled
        assert len(calls) == 1
        with pytest.raises(StateError):
            carrier.execute(invocation, io=CarrierIO(), deadline=Deadline.after(0), custody=custody)
        with pytest.raises(StateError):
            carrier._wire.request_power(timeout=1, custody=custody)
        assert len(calls) == 1
    finally:
        release.set()
        assert custody.close(Deadline.after(3))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_real_http_worker_binary_json_is_parsed_privately(monkeypatch: pytest.MonkeyPatch, encoding: str) -> None:
    original = subprocess.Popen
    calls: list[tuple[object, dict[str, Any]]] = []
    body = '{"data":{"pid":42}}'.encode(encoding)

    def worker(argv: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        calls.append((argv, kwargs))
        script = (
            f"import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write({body!r}); sys.stderr.write('secret-canary')"
        )
        return original([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", worker)
    custody = LocalDeliveryCustody()
    try:
        assert _ProxmoxWire(connection()).request("POST", "exec", body=b"{}", timeout=3, custody=custody) == {"pid": 42}
        assert custody.settled
        assert len(calls) == 1
        assert "secret-canary" not in repr(calls)
    finally:
        assert custody.close(Deadline.after(3))


def test_real_http_worker_invalid_binary_output_has_no_sensitive_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen

    def worker(argv: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        return original(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'\\xffsecret-canary')",
            ],
            **kwargs,
        )

    monkeypatch.setattr(subprocess, "Popen", worker)
    custody = LocalDeliveryCustody()
    try:
        with pytest.raises(_WireFailure) as caught:
            _ProxmoxWire(connection()).request("GET", "exec-status?pid=42", timeout=3, custody=custody)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert "secret-canary" not in str(caught.value)
    finally:
        assert custody.close(Deadline.after(3))


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_delayed_admission_interruption_retains_owner_and_original_control(
    monkeypatch: pytest.MonkeyPatch, control_type: type[BaseException]
) -> None:
    original = subprocess.Popen
    admit = core.LocalProcessOwner._admit
    entered = threading.Event()
    release = threading.Event()
    control = control_type()

    def delayed(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def interrupted(owner: core.LocalProcessOwner, request: core.LocalProcessRequest) -> bool:
        admit(owner, request)
        assert entered.wait(3)
        raise control

    monkeypatch.setattr(subprocess, "Popen", delayed)
    monkeypatch.setattr(core.LocalProcessOwner, "_admit", interrupted)
    custody = LocalDeliveryCustody()
    started = time.monotonic()
    try:
        with pytest.raises(control_type) as caught:
            run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                io=CarrierIO(),
                deadline=Deadline(None),
                custody=custody,
            )
        assert caught.value is control
        assert time.monotonic() - started < 2
        assert not custody.settled
        with pytest.raises(StateError):
            custody.begin_process()
    finally:
        release.set()
        assert custody.close(Deadline.after(3))


@pytest.mark.parametrize("owner_provided", [False, True])
def test_host_cleanup_parameters_are_explicitly_paired(owner_provided: bool) -> None:
    with pytest.raises(ValueError):
        core.run_owned_process(
            [sys.executable, "-c", "pass"],
            input=core.ProcessInput(),
            output=core.ProcessOutput(),
            deadline=core.Deadline(None),
            owner=core.LocalProcessOwner() if owner_provided else None,
            cleanup_allowance=None if owner_provided else 0.5,
        )


def test_multiple_independent_http_workers_have_no_shared_custody(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen
    barrier = threading.Barrier(3)

    def worker(argv: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        barrier.wait(timeout=3)
        script = 'import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b\'{"data":{"pid":42}}\')'
        return original([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", worker)
    stores = [LocalDeliveryCustody() for _ in range(3)]

    def exchange(custody: LocalDeliveryCustody) -> dict[str, object]:
        return _ProxmoxWire(connection()).request("POST", "exec", body=b"{}", timeout=5, custody=custody)

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            assert list(executor.map(exchange, stores)) == [{"pid": 42}] * 3
        assert all(custody.settled for custody in stores)
    finally:
        for custody in stores:
            assert custody.close(Deadline.after(3))


def test_private_http_capture_enforces_complete_response_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen
    monkeypatch.setattr("agentworks.execution.carriers.proxmox._MAX_RESPONSE_BYTES", 10)

    def worker(argv: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        script = 'import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b\'{"data":{"pid":42}}\')'
        return original([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", worker)
    custody = LocalDeliveryCustody()
    try:
        with pytest.raises(_WireFailure):
            _ProxmoxWire(connection()).request("POST", "exec", body=b"{}", timeout=3, custody=custody)
        assert custody.settled

    finally:
        assert custody.close(Deadline.after(3))
