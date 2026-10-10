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

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.execution import _process as core
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import CarrierIO, Deadline, Dispatch, Failure, PreparedInvocation
from agentworks.execution.carriers._subprocess import run_process
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, _ProxmoxWire, _WireFailure
from agentworks.operations import OperationOwner
from tests.execution.test_proxmox import connection

pytestmark = pytest.mark.windows


class Cleanup:
    def __init__(self, owner: core.LocalProcessOwner) -> None:
        self.owner = owner
        self.borrowers_ready = True
        self.process_ready = True
        self.restore_ready = True
        self.complete = False
        self.events: list[str] = []
        self.deadlines: list[Deadline] = []

    @property
    def settled(self) -> bool:
        return self.complete

    def close(self, deadline: Deadline) -> None:
        self.deadlines.append(deadline)
        if not self.borrowers_ready:
            return
        self.events.append("borrowers-stopped")
        if not self.process_ready:
            return
        terminal = self.owner.close_bounded(core.Deadline(deadline.expires_at))
        if terminal is None or not terminal.cleaned:
            return
        self.events.append("process-cleaned")
        if self.restore_ready:
            self.events.append("resources-restored")
            self.complete = True


def test_coordinator_binding_and_observation_have_no_resource_effects() -> None:
    custody = LocalDeliveryCustody()
    owner = custody.begin_process()
    cleanup = Cleanup(owner)
    custody.retain_cleanup(owner, cleanup)
    for _ in range(3):
        assert not custody.settled
    assert owner.snapshot().terminal is None
    assert cleanup.events == [] and cleanup.deadlines == []
    cleanup.complete = True
    assert not custody.settled  # Coordinator completion alone cannot prove native cleanup.
    owner.close_bounded(core.Deadline(time.monotonic() + 1))
    assert custody.settled


def test_cleanup_requires_current_exact_owner_without_replacing_existing_coordinator() -> None:
    custody = LocalDeliveryCustody()
    foreign = LocalDeliveryCustody().begin_process()
    cleanup = Cleanup(foreign)
    with pytest.raises(StateError):
        custody.retain_cleanup(foreign, cleanup)
    first = custody.begin_process()
    with pytest.raises(StateError):
        custody.retain_cleanup(foreign, cleanup)
    held = Cleanup(first)
    custody.retain_cleanup(first, held)
    assert custody.close(Deadline.after(1))
    with pytest.raises(StateError):
        custody.retain_cleanup(first, Cleanup(first))
    with pytest.raises(StateError):
        custody.retain_cleanup(first, held)
    second = custody.begin_process()
    assert second is not first and not custody.settled
    with pytest.raises(StateError):
        custody.retain_cleanup(first, Cleanup(first))
    following = Cleanup(second)
    custody.retain_cleanup(second, following)
    assert custody.close(Deadline.after(1))
    assert len(held.deadlines) == len(following.deadlines) == 1
    assert cleanup.events == []


def test_lost_begin_reply_keeps_new_owner_reachable_without_old_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    custody = LocalDeliveryCustody()
    first = custody.begin_process()
    cleanup = Cleanup(first)
    custody.retain_cleanup(first, cleanup)
    assert custody.close(Deadline.after(1))
    original_deadlines = tuple(cleanup.deadlines)
    control = KeyboardInterrupt("owner publication reply lost")
    cause = OSError("original cause")
    control.__cause__ = cause
    published: list[core.LocalProcessOwner] = []

    def publish(self: LocalDeliveryCustody, name: str, value: object) -> None:
        object.__setattr__(self, name, value)
        if (
            self is custody
            and isinstance(value, core.LocalProcessOwner)
            and value is not first
            and self._owner is value  # noqa: SLF001
            and not published
        ):
            published.append(value)
            raise control

    def forbid_dispatch(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("inert owner reset must not admit native dispatch")

    with monkeypatch.context() as fault:
        fault.setattr(LocalDeliveryCustody, "__setattr__", publish)
        fault.setattr(subprocess, "Popen", forbid_dispatch)
        with pytest.raises(KeyboardInterrupt) as raised:
            custody.begin_process()
        assert raised.value is control and control.__cause__ is cause
        (held,) = published
        assert held.snapshot().terminal is None and not custody.settled
        with pytest.raises(StateError):
            custody.begin_process()
        assert custody._owner is held  # noqa: SLF001
        assert custody.close(Deadline.after(1))
        terminal = held.snapshot().terminal
        assert terminal is not None and terminal.cleaned
        assert tuple(cleanup.deadlines) == original_deadlines
        assert custody._owner is held  # noqa: SLF001


def test_coordinator_blocks_native_close_until_borrowers_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    custody = LocalDeliveryCustody()
    owner = custody.begin_process()
    cleanup = Cleanup(owner)
    cleanup.borrowers_ready = False
    custody.retain_cleanup(owner, cleanup)
    original = owner.close_bounded
    calls: list[core.Deadline] = []

    def native_close(deadline: core.Deadline) -> core.LocalProcessTerminal | None:
        assert cleanup.events[-1] == "borrowers-stopped"
        calls.append(deadline)
        return original(deadline)

    monkeypatch.setattr(owner, "close_bounded", native_close)
    with pytest.raises(ValidationError):
        custody.close(Deadline(None))
    assert cleanup.deadlines == [] and calls == []
    expired = Deadline.after(0)
    assert not custody.close(expired)
    assert cleanup.deadlines == [expired] and cleanup.deadlines[0] is expired
    assert calls == [] and owner.snapshot().terminal is None
    cleanup.borrowers_ready = True
    fresh = Deadline.after(1)
    assert custody.close(fresh)
    assert cleanup.deadlines[-1] is fresh and calls[0].expires_at == fresh.expires_at
    assert cleanup.events == ["borrowers-stopped", "process-cleaned", "resources-restored"]


@pytest.mark.parametrize("pending", ("process", "restoration"))
def test_aggregate_cleanup_blocks_dispatch_and_retries_same_coordinator(pending: str) -> None:
    custody = LocalDeliveryCustody()
    owner = custody.begin_process()
    cleanup = Cleanup(owner)
    cleanup.process_ready = pending != "process"
    cleanup.restore_ready = pending != "restoration"
    custody.retain_cleanup(owner, cleanup)
    initial = Deadline.after(0)
    assert not custody.close(initial)
    terminal = owner.snapshot().terminal
    assert (terminal is not None and terminal.cleaned) is (pending == "restoration")
    assert not custody.settled
    with pytest.raises(StateError):
        custody.begin_process()
    cleanup.process_ready = cleanup.restore_ready = True
    fresh = Deadline.after(1)
    assert custody.close(fresh)
    assert cleanup.deadlines == [initial, fresh]
    assert cleanup.deadlines[0] is initial and cleanup.deadlines[1] is fresh
    assert custody.begin_process() is not owner
    assert cleanup.deadlines == [initial, fresh]  # Reset never invokes old resource cleanup.
    assert custody.close(Deadline.after(1))


@pytest.mark.parametrize("error_type", (RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit))
@pytest.mark.parametrize("process_cleaned", (False, True))
def test_cleanup_error_preserves_identity_cause_and_same_coordinator_for_retry(
    monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException], process_cleaned: bool
) -> None:
    custody = LocalDeliveryCustody()
    owner = custody.begin_process()
    cleanup = Cleanup(owner)
    custody.retain_cleanup(owner, cleanup)
    error = error_type("cleanup interrupted")
    cause = OSError("original cause")
    error.__cause__ = cause

    def interrupt(deadline: Deadline) -> None:
        cleanup.deadlines.append(deadline)
        if process_cleaned:
            owner.close_bounded(core.Deadline(deadline.expires_at))
        raise error

    with monkeypatch.context() as fault:
        fault.setattr(cleanup, "close", interrupt)
        with pytest.raises(error_type) as raised:
            custody.close(Deadline.after(1))
    assert raised.value is error and error.__cause__ is cause
    terminal = owner.snapshot().terminal
    assert (terminal is not None and terminal.cleaned) is process_cleaned
    assert not custody.settled
    with pytest.raises(StateError):
        custody.begin_process()
    with pytest.raises(StateError):
        custody.retain_cleanup(owner, Cleanup(owner))
    fresh = Deadline.after(1)
    assert custody.close(fresh)
    assert len(cleanup.deadlines) == 2 and cleanup.deadlines[-1] is fresh


@pytest.mark.parametrize("lost_status", [False, True])
def test_coordinator_survives_operation_attempt_handoff(tmp_path, monkeypatch, lost_status: bool) -> None:
    if lost_status and os.name != "posix":
        pytest.skip("exact wait loss is POSIX-only")
    database = Database(tmp_path / "state.db")
    try:
        operation = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, "fixture"), "local-cleanup"
        )
        borrow = operation.borrow()
        attempt = borrow.begin_attempt()
        owner = attempt.local_delivery.begin_process()
        cleanup = Cleanup(owner)
        cleanup.restore_ready = False
        attempt.local_delivery.retain_cleanup(owner, cleanup)
        if lost_status:
            spawn = subprocess.Popen

            def externally_reap(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
                child = spawn(*args, **kwargs)
                os.waitpid(child.pid, 0)
                return child

            monkeypatch.setattr(subprocess, "Popen", externally_reap)
            owner.start(core.LocalProcessRequest((sys.executable, "-c", "pass"), core.LocalProcessInput.EOF))
        assert not attempt.local_delivery.close(Deadline.after(1))
        terminal = owner.snapshot().terminal
        assert terminal is not None and terminal.cleaned
        if lost_status:
            assert terminal.observation_failed and terminal.local_status is terminal.exit_status is None
        with pytest.raises(StateError):
            attempt.settle()
        borrow.handoff_unresolved()
        assert not operation.close_local_delivery(Deadline.after(0))
        with pytest.raises(StateError):
            operation.borrow()
        cleanup.restore_ready = True
        assert operation.close_local_delivery(Deadline.after(1))
        assert len(cleanup.deadlines) == 3 and cleanup.complete
        with pytest.raises(StateError):
            operation.borrow()  # Local cleanup never clears unresolved operation effects.
    finally:
        database.close()


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
def test_lost_status_remains_unknown_after_local_retirement_and_custody_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert result.local_status is result.exit_status is None
    assert custody.settled
    assert custody.close(Deadline.after(1))
    assert custody.close(Deadline.after(1))
    replacement = custody.begin_process()
    assert replacement.close().cleaned
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


def test_runner_requires_an_external_owner() -> None:
    with pytest.raises(TypeError):
        core.run_owned_process(  # type: ignore[call-arg]
            [sys.executable, "-c", "pass"],
            input=core.ProcessInput(),
            output=core.ProcessOutput(),
            deadline=core.Deadline(None),
            cleanup_allowance=0.5,
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
