"""Portable evidence tests for the private WSL2 guest-anchor candidate."""

from __future__ import annotations

import os
import selectors
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _wsl2_lifecycle as lifecycle
from agentworks.execution._wsl2_lifecycle import (
    _HELPER_SOURCE,
    GuestAnchorIdentity,
    GuestAnchorPresence,
    HandleSettlement,
    HelperExitReceipt,
    HostClientStatus,
    JobAssignment,
    LocalResourceSnapshot,
    WSL2GuestAnchorOwner,
)
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Connection


def local(
    host: HostClientStatus = HostClientStatus.NOT_CREATED,
    exit_status: int | None = None,
    assignment: JobAssignment = JobAssignment.NOT_CREATED,
    job: HandleSettlement = HandleSettlement.NOT_CREATED,
    handles: HandleSettlement | None = None,
) -> LocalResourceSnapshot:
    if handles is None:
        handles = HandleSettlement.NOT_CREATED if host == HostClientStatus.NOT_CREATED else HandleSettlement.OPEN
    return LocalResourceSnapshot(host, exit_status, assignment, job, handles)


@dataclass
class FakeNative:
    lines: list[object]
    local_state: LocalResourceSnapshot = field(default_factory=local)
    spawn_state: LocalResourceSnapshot = field(
        default_factory=lambda: local(
            HostClientStatus.ACTIVE,
            assignment=JobAssignment.ASSIGNED_AT_CREATION,
            job=HandleSettlement.OPEN,
        )
    )
    settle_results: list[LocalResourceSnapshot | BaseException] = field(default_factory=list)
    wait_statuses: list[int | None] = field(default_factory=lambda: [0])
    spawn_interrupt: BaseException | None = None
    close_interrupt: BaseException | None = None
    close_fails_without_eof: bool = False
    snapshot_interrupt_at: int | None = None
    argv: tuple[str, ...] = ()
    spawned: bool = False
    closed_stdin: bool = False
    snapshot_calls: int = 0
    settle_deadlines: list[Deadline] = field(default_factory=list)

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None:
        assert not deadline.expired
        self.argv = argv
        self.spawned = True
        self.local_state = self.spawn_state
        if self.spawn_interrupt is not None:
            raise self.spawn_interrupt

    def read_stdout_line(self, limit: int, deadline: Deadline) -> bytes:
        assert limit == 513
        assert not deadline.expired
        value = self.lines.pop(0)
        return value  # type: ignore[return-value]

    def close_stdin(self) -> None:
        if self.close_interrupt is not None:
            raise self.close_interrupt
        if self.close_fails_without_eof:
            raise OSError
        self.closed_stdin = True

    def wait(self, deadline: Deadline) -> int | None:
        assert not deadline.expired
        status = self.wait_statuses.pop(0) if self.wait_statuses else None
        if type(status) is int:
            self.local_state = replace(
                self.local_state,
                host_client_status=HostClientStatus.EXITED,
                host_client_exit_status=status,
            )
        return status

    def snapshot(self) -> LocalResourceSnapshot:
        self.snapshot_calls += 1
        if self.snapshot_interrupt_at == self.snapshot_calls:
            raise KeyboardInterrupt
        return self.local_state

    def settle(self, deadline: Deadline) -> LocalResourceSnapshot:
        assert not deadline.expired
        self.settle_deadlines.append(deadline)
        result = self.settle_results.pop(0) if self.settle_results else self.local_state
        if isinstance(result, BaseException):
            raise result
        self.local_state = result
        return result


@dataclass
class FakeObserver:
    results: list[GuestAnchorPresence]
    seen: list[GuestAnchorIdentity] = field(default_factory=list)

    def observe(self, identity: GuestAnchorIdentity, deadline: Deadline) -> GuestAnchorPresence:
        assert not deadline.expired
        self.seen.append(identity)
        return self.results.pop(0)


def connection() -> WSL2Connection:
    return WSL2Connection("test-distro", "test-user", r"C:\Windows\System32\wsl.exe")


def ready(nonce: str = "a" * 32) -> bytes:
    return f"READY {nonce} 137 8192\n".encode()


def owner(native: FakeNative, observer: FakeObserver | None = None) -> WSL2GuestAnchorOwner:
    return WSL2GuestAnchorOwner(connection(), native, observer=observer)


def token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle.secrets, "token_hex", lambda size: "a" * (size * 2))  # type: ignore[attr-defined]


def test_literal_helper_argv_and_start_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    native = FakeNative([ready()])
    subject = owner(native)

    evidence = subject.start(Deadline.after(1))

    assert native.argv[:7] == (
        r"C:\Windows\System32\wsl.exe",
        "--distribution",
        "test-distro",
        "--user",
        "test-user",
        "--exec",
        "/usr/bin/python3",
    )
    assert native.argv[7:12] == ("-I", "-S", "-B", "-c", _HELPER_SOURCE)
    assert native.argv[12] == "a" * 32
    assert evidence.identity == GuestAnchorIdentity(137, 8192)
    assert evidence.local.job_assignment == JobAssignment.ASSIGNED_AT_CREATION


def _bounded_line(selector: selectors.BaseSelector, stream: IO[bytes]) -> bytes:
    selector.register(stream, selectors.EVENT_READ)
    try:
        expires_at = time.monotonic() + 2
        received = bytearray()
        while len(received) < 513 and not received.endswith(b"\n"):
            remaining = expires_at - time.monotonic()
            assert remaining > 0
            assert selector.select(timeout=remaining)
            chunk = os.read(stream.fileno(), 513 - len(received))
            if not chunk:
                break
            received.extend(chunk)
        return bytes(received)
    finally:
        selector.unregister(stream)


def test_helper_protocol_runs_under_local_python_and_waits_for_eof() -> None:
    if not Path("/proc/self/stat").is_file():
        pytest.skip("requires procfs")
    nonce = "a" * 32
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-c", _HELPER_SOURCE, nonce],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            line = _bounded_line(selector, process.stdout)
            _, received_nonce, pid_text, start_text = line.decode("ascii").split()
            assert received_nonce == nonce
            pid = int(pid_text)
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            assert int(stat[stat.rfind(")") + 2 :].split()[19]) == int(start_text)
            assert process.poll() is None
            process.stdin.close()
            assert _bounded_line(selector, process.stdout) == f"EXITING {nonce}\n".encode()
        assert process.wait(timeout=2) == 0
        assert process.stderr.read() == b""
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with suppress(OSError):
                    stream.close()


def test_snapshot_requires_both_host_and_job_settlement() -> None:
    assert not local(HostClientStatus.EXITED, 0, JobAssignment.ASSIGNED_AT_CREATION, HandleSettlement.OPEN).settled
    assert not local(
        HostClientStatus.ACTIVE,
        None,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    ).settled
    assert not local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.OPEN,
    ).settled
    assert local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    ).settled
    assert local().settled


def test_job_handle_uncertainty_prevents_settlement_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    uncertain = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.UNKNOWN,
        HandleSettlement.CLOSED,
    )
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative([ready(), b"EXITING " + b"a" * 32 + b"\n"], settle_results=[uncertain, settled])
    subject = owner(native)
    subject.start(Deadline.after(1))

    first = subject.release(Deadline.after(1))
    second = subject.release(Deadline.after(1))

    assert not first.local.settled
    assert second.local.settled
    assert len(native.settle_deadlines) == 2


def test_host_handle_uncertainty_prevents_settlement_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    uncertain = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.OPEN,
    )
    settled = replace(uncertain, host_handle_settlement=HandleSettlement.CLOSED)
    native = FakeNative([ready(), b""], settle_results=[uncertain, settled])
    subject = owner(native)
    subject.start(Deadline.after(1))

    first = subject.release(Deadline.after(1))
    second = subject.release(Deadline.after(1))

    assert not first.local.settled
    assert second.local.settled
    assert len(native.settle_deadlines) == 2


def test_local_settlement_and_helper_receipt_do_not_prove_guest_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative([ready(), b"EXITING " + b"a" * 32 + b"\n"], settle_results=[settled])
    subject = owner(native)
    subject.start(Deadline.after(1))

    evidence = subject.release(Deadline.after(1))

    assert evidence.local.settled
    assert evidence.helper_exit_receipt == HelperExitReceipt.RECEIVED
    assert evidence.guest_anchor_presence == GuestAnchorPresence.UNKNOWN


def test_release_retries_guest_observer_after_local_settlement(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative([ready(), b""], settle_results=[settled])
    observer = FakeObserver([GuestAnchorPresence.PRESENT, GuestAnchorPresence.ABSENT_CONFIRMED])
    subject = owner(native, observer)
    subject.start(Deadline.after(1))

    first = subject.release(Deadline.after(1))
    second = subject.release(Deadline.after(1))

    assert first.guest_anchor_presence == GuestAnchorPresence.PRESENT
    assert second.guest_anchor_presence == GuestAnchorPresence.ABSENT_CONFIRMED
    assert observer.seen == [GuestAnchorIdentity(137, 8192), GuestAnchorIdentity(137, 8192)]


@pytest.mark.parametrize(
    "receipt",
    [b"READY " + b"b" * 32 + b" 137 8192\n", b"READY " + b"a" * 32 + b" nope 8192\n", b"x" * 513],
)
def test_invalid_ready_retains_retryable_owner_and_settles(monkeypatch: pytest.MonkeyPatch, receipt: bytes) -> None:
    token(monkeypatch)
    uncertain = local(HostClientStatus.UNKNOWN, None, JobAssignment.UNKNOWN, HandleSettlement.OPEN)
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.FAILED,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative([receipt], settle_results=[uncertain, settled])
    subject = owner(native)

    with pytest.raises(ValidationError) as raised:
        subject.start(Deadline.after(1))

    assert subject.evidence.identity is None
    assert not subject.evidence.local.settled
    assert raised.value.__notes__
    assert subject.settle().local.settled


def test_spawn_interruption_preserves_original_and_owner_can_retry_settlement(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    interrupted = KeyboardInterrupt()
    uncertain = local(HostClientStatus.UNKNOWN, None, JobAssignment.UNKNOWN, HandleSettlement.OPEN)
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.UNKNOWN,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative([], spawn_interrupt=interrupted, settle_results=[uncertain, settled])
    subject = owner(native)

    with pytest.raises(KeyboardInterrupt) as raised:
        subject.start(Deadline.after(1))

    assert raised.value is interrupted
    assert native.spawned and not subject.evidence.local.settled
    assert subject.settle().local.settled


def test_lost_spawn_cleanup_and_snapshot_never_reuses_predispatch_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token(monkeypatch)
    interrupted = KeyboardInterrupt()
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.UNKNOWN,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative(
        [],
        spawn_interrupt=interrupted,
        settle_results=[OSError(), settled],
        snapshot_interrupt_at=2,
    )
    subject = owner(native)

    with pytest.raises(KeyboardInterrupt) as raised:
        subject.start(Deadline.after(1))

    assert raised.value is interrupted
    assert not subject.evidence.local.settled
    assert raised.value.__notes__
    assert subject.release(Deadline.after(0)).local.settled
    assert len(native.settle_deadlines) == 2


def test_interruption_after_ready_publication_retains_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative([ready()], settle_results=[settled], snapshot_interrupt_at=3)
    subject = owner(native)

    with pytest.raises(KeyboardInterrupt):
        subject.start(Deadline.after(1))

    assert subject.evidence.identity == GuestAnchorIdentity(137, 8192)
    assert subject.evidence.local.settled


def test_late_ready_is_rejected_and_cleaned(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative([ready()], settle_results=[settled])
    subject = owner(native)
    original = native.read_stdout_line
    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def late(limit: int, deadline: Deadline) -> bytes:
        result = original(limit, deadline)
        clock[0] = 12.0
        return result

    native.read_stdout_line = late  # type: ignore[method-assign]
    with pytest.raises(ValidationError):
        subject.start(Deadline.after(1))

    assert subject.evidence.identity == GuestAnchorIdentity(137, 8192)
    assert subject.evidence.local.settled


def test_expired_operation_deadline_gets_one_fresh_bounded_cleanup_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token(monkeypatch)
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative([ready()], settle_results=[settled])
    subject = owner(native)
    subject.start(Deadline.after(1))

    evidence = subject.release(Deadline.after(0))

    assert evidence.local.settled
    assert len(native.settle_deadlines) == 1
    remaining = native.settle_deadlines[0].remaining()
    assert remaining is not None and 0 < remaining <= 0.5


def test_release_preserves_control_interruption_after_bounded_settlement(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    interrupted = KeyboardInterrupt()
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative([ready()], close_interrupt=interrupted, settle_results=[settled])
    subject = owner(native)
    subject.start(Deadline.after(1))

    with pytest.raises(KeyboardInterrupt) as raised:
        subject.release(Deadline.after(1))

    assert raised.value is interrupted
    assert subject.evidence.local.settled


def test_normal_release_reraises_native_settlement_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    interrupted = KeyboardInterrupt()
    native = FakeNative([ready(), b""], settle_results=[interrupted])
    subject = owner(native)
    subject.start(Deadline.after(1))

    with pytest.raises(KeyboardInterrupt) as raised:
        subject.release(Deadline.after(1))

    assert raised.value is interrupted
    assert not subject.evidence.local.settled
    assert len(native.settle_deadlines) == 1


def test_direct_settle_refreshes_evidence_before_reraising_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    interrupted = KeyboardInterrupt()
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.ASSIGNED_AT_CREATION,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative([ready()], settle_results=[interrupted])
    subject = owner(native)
    subject.start(Deadline.after(1))
    native.local_state = settled

    with pytest.raises(KeyboardInterrupt) as raised:
        subject.settle()

    assert raised.value is interrupted
    assert subject.evidence.local.settled


def test_close_failure_does_not_claim_eof_before_local_settlement(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    settled = local(
        HostClientStatus.EXITED,
        0,
        JobAssignment.FAILED,
        HandleSettlement.CLOSED,
        HandleSettlement.CLOSED,
    )
    native = FakeNative([ready()], close_fails_without_eof=True, settle_results=[settled])
    subject = owner(native)
    subject.start(Deadline.after(1))

    evidence = subject.release(Deadline.after(1))

    assert not native.closed_stdin
    assert evidence.helper_exit_receipt == HelperExitReceipt.NOT_OBSERVED
    assert evidence.local.settled
    assert all(not deadline.expired for deadline in native.settle_deadlines)


def test_import_does_not_load_legacy_wsl_or_transport_modules() -> None:
    script = r"""
import sys
class Blocker:
    def find_spec(self, fullname, path=None, target=None):
        blocked = ('agentworks.transports', 'agentworks.capabilities.vm_platform.wsl2')
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Blocker())
import agentworks.execution._wsl2_lifecycle
"""
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
