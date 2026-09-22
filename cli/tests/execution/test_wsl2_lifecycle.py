"""Portable evidence tests for the private WSL2 guest-anchor candidate."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._wsl2_lifecycle import (
    _HELPER_SOURCE,
    GuestAnchorIdentity,
    GuestAnchorPresence,
    HelperExitReceipt,
    HostClientStatus,
    JobAssignment,
    WSL2GuestAnchorLauncher,
)
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Connection


@dataclass
class FakeProcess:
    receipts: list[object]
    pid: int = 43
    exit_status: int | None = 0
    close_fails: bool = False
    read_fails: bool = False
    read_interrupt: BaseException | None = None
    wait_fails: bool = False
    terminate_fails: bool = False
    closed_stdin: bool = False
    terminated: bool = False

    def read_stdout(self, limit: int, deadline: Deadline) -> bytes:
        assert limit == 513
        assert not deadline.expired
        if self.read_interrupt is not None:
            raise self.read_interrupt
        if self.read_fails:
            raise OSError
        value = self.receipts.pop(0)
        if type(value) is not bytes:
            return value  # type: ignore[return-value]
        return value

    def close_stdin(self) -> None:
        self.closed_stdin = True
        if self.close_fails:
            raise OSError

    def wait(self, deadline: Deadline) -> int | None:
        assert not deadline.expired
        if self.wait_fails:
            raise OSError
        return self.exit_status

    def terminate(self, deadline: Deadline) -> None:
        assert not deadline.expired
        self.terminated = True
        if self.terminate_fails:
            raise OSError


@dataclass
class FakeHost:
    process: FakeProcess | None = None
    interrupt: BaseException | None = None
    argv: tuple[str, ...] | None = None

    def spawn(self, argv: tuple[str, ...]) -> FakeProcess:
        self.argv = argv
        if self.interrupt is not None:
            raise self.interrupt
        assert self.process is not None
        return self.process


@dataclass
class FakeJob:
    assign_interrupt: BaseException | None = None
    assign_fails: bool = False
    close_fails: bool = False
    assigned: list[FakeProcess] = field(default_factory=list)
    closed: bool = False

    def assign(self, process: FakeProcess) -> None:
        self.assigned.append(process)
        if self.assign_interrupt is not None:
            raise self.assign_interrupt
        if self.assign_fails:
            raise OSError

    def close(self, deadline: Deadline) -> None:
        assert not deadline.expired
        self.closed = True
        if self.close_fails:
            raise OSError


@dataclass
class FakeJobs:
    job: FakeJob | None = field(default_factory=FakeJob)
    unavailable: bool = False

    def create(self) -> FakeJob:
        if self.unavailable:
            raise OSError
        assert self.job is not None
        return self.job


@dataclass
class FakeObserver:
    result: object
    seen: list[GuestAnchorIdentity] = field(default_factory=list)

    def observe(self, identity: GuestAnchorIdentity, deadline: Deadline) -> GuestAnchorPresence:
        assert not deadline.expired
        self.seen.append(identity)
        return self.result  # type: ignore[return-value]


def connection() -> WSL2Connection:
    return WSL2Connection("test-distro", "test-user", r"C:\Windows\System32\wsl.exe")


def ready(nonce: str = "a" * 32) -> bytes:
    return f"READY {nonce} 137 8192\n".encode()


def launcher(
    process: FakeProcess,
    jobs: FakeJobs | None = None,
    observer: FakeObserver | None = None,
    nonce: object = "a" * 32,
) -> tuple[WSL2GuestAnchorLauncher, FakeHost, FakeJobs]:
    host = FakeHost(process)
    actual_jobs = jobs or FakeJobs()
    return (
        WSL2GuestAnchorLauncher(
            connection(),
            host=host,
            jobs=actual_jobs,
            observer=observer,
            nonce_factory=cast(Callable[[], str], lambda: nonce),
        ),
        host,
        actual_jobs,
    )


def test_launch_uses_one_fixed_literal_python_helper_argv() -> None:
    process = FakeProcess([ready(), b"EXITING " + b"a" * 32 + b"\n"])
    subject, host, _ = launcher(process)

    anchor = subject.start(Deadline.after(1))

    assert host.argv is not None
    assert host.argv[:7] == (
        r"C:\Windows\System32\wsl.exe",
        "--distribution",
        "test-distro",
        "--user",
        "test-user",
        "--exec",
        "/usr/bin/python3",
    )
    assert host.argv[7:12] == ("-I", "-S", "-B", "-c", _HELPER_SOURCE)
    assert host.argv[12] == "a" * 32
    assert anchor.evidence.job_assignment == JobAssignment.ASSIGNED
    assert anchor.evidence.pre_assignment_orphan_window


def test_ready_identity_and_exit_receipt_are_not_guest_absence_evidence() -> None:
    process = FakeProcess([ready(), b"EXITING " + b"a" * 32 + b"\n"])
    subject, _, _ = launcher(process)
    anchor = subject.start(Deadline.after(1))

    evidence = anchor.release(Deadline.after(1))

    assert process.closed_stdin
    assert evidence.identity == GuestAnchorIdentity(137, 8192)
    assert evidence.helper_exit_receipt == HelperExitReceipt.RECEIVED
    assert evidence.host_client_status == HostClientStatus.EXITED
    assert evidence.host_client_exit_status == 0
    assert evidence.guest_anchor_presence == GuestAnchorPresence.UNKNOWN


def test_eof_without_helper_exit_receipt_is_not_a_malformed_receipt() -> None:
    process = FakeProcess([ready(), b""])
    subject, _, _ = launcher(process)
    anchor = subject.start(Deadline.after(1))

    evidence = anchor.release(Deadline.after(1))

    assert evidence.helper_exit_receipt == HelperExitReceipt.NOT_OBSERVED


@pytest.mark.parametrize("presence", list(GuestAnchorPresence))
def test_only_injected_exact_identity_observer_supplies_guest_presence(presence: GuestAnchorPresence) -> None:
    process = FakeProcess([ready(), b""])
    observer = FakeObserver(presence)
    subject, _, _ = launcher(process, observer=observer)
    anchor = subject.start(Deadline.after(1))

    evidence = anchor.release(Deadline.after(1))

    assert observer.seen == [GuestAnchorIdentity(137, 8192)]
    assert evidence.guest_anchor_presence == presence


def test_job_unavailability_and_assignment_failure_remain_visible() -> None:
    unavailable_process = FakeProcess([ready(), b""])
    unavailable, _, _ = launcher(unavailable_process, jobs=FakeJobs(unavailable=True))
    unavailable_anchor = unavailable.start(Deadline.after(1))
    failed_process = FakeProcess([ready(), b""])
    failed, _, _ = launcher(failed_process, jobs=FakeJobs(job=FakeJob(assign_fails=True)))
    failed_anchor = failed.start(Deadline.after(1))

    assert unavailable_anchor.evidence.job_assignment == JobAssignment.NOT_AVAILABLE
    assert failed_anchor.evidence.job_assignment == JobAssignment.FAILED
    assert unavailable_anchor.evidence.pre_assignment_orphan_window
    assert failed_anchor.evidence.pre_assignment_orphan_window


@pytest.mark.parametrize(
    "receipt",
    [b"READY " + b"b" * 32 + b" 137 8192\n", b"READY " + b"a" * 32 + b" nope 8192\n", b"x" * 513],
)
def test_malformed_foreign_and_oversized_ready_records_abort_with_cleanup(receipt: bytes) -> None:
    process = FakeProcess([receipt])
    subject, _, jobs = launcher(process)

    with pytest.raises(ValidationError):
        subject.start(Deadline.after(1))

    assert process.terminated
    assert jobs.job is not None and jobs.job.closed


def test_interruption_after_spawn_preserves_original_exception_and_attempts_cleanup() -> None:
    interrupted = KeyboardInterrupt()
    process = FakeProcess([ready()], read_interrupt=interrupted, terminate_fails=True)
    subject, _, jobs = launcher(process)

    with pytest.raises(KeyboardInterrupt) as raised:
        subject.start(Deadline.after(1))

    assert raised.value is interrupted
    assert process.terminated
    assert jobs.job is not None and jobs.job.closed
    assert raised.value.__notes__


def test_interruption_before_spawn_preserves_original_exception_without_cleanup_capability() -> None:
    interrupted = KeyboardInterrupt()
    host = FakeHost(interrupt=interrupted)
    jobs = FakeJobs()
    subject = WSL2GuestAnchorLauncher(connection(), host=host, jobs=jobs, nonce_factory=lambda: "a" * 32)

    with pytest.raises(KeyboardInterrupt) as raised:
        subject.start(Deadline.after(1))

    assert raised.value is interrupted
    assert jobs.job is not None and not jobs.job.closed


def test_interruption_during_assignment_is_uncertain_and_preserves_original_exception() -> None:
    interrupted = KeyboardInterrupt()
    process = FakeProcess([ready()])
    jobs = FakeJobs(job=FakeJob(assign_interrupt=interrupted))
    subject, _, _ = launcher(process, jobs=jobs)

    with pytest.raises(KeyboardInterrupt) as raised:
        subject.start(Deadline.after(1))

    assert raised.value is interrupted
    assert process.terminated
    assert jobs.job is not None and jobs.job.closed


def test_release_cleanup_failure_is_evidence_not_a_guest_absence_claim() -> None:
    process = FakeProcess([ready(), b"not a receipt"], close_fails=True, wait_fails=True)
    jobs = FakeJobs(job=FakeJob(close_fails=True))
    subject, _, _ = launcher(process, jobs=jobs)
    anchor = subject.start(Deadline.after(1))

    evidence = anchor.release(Deadline.after(1))

    assert evidence.cleanup_failed
    assert evidence.helper_exit_receipt == HelperExitReceipt.INVALID
    assert evidence.host_client_status == HostClientStatus.NOT_OBSERVED
    assert evidence.guest_anchor_presence == GuestAnchorPresence.UNKNOWN


def test_release_interruption_preserves_original_exception_after_emergency_cleanup() -> None:
    interrupted = KeyboardInterrupt()
    process = FakeProcess([ready(), b""], wait_fails=True)
    jobs = FakeJobs(job=FakeJob(close_fails=True))
    subject, _, _ = launcher(process, jobs=jobs)
    anchor = subject.start(Deadline.after(1))
    process.close_stdin = lambda: (_ for _ in ()).throw(interrupted)  # type: ignore[method-assign]

    with pytest.raises(KeyboardInterrupt) as raised:
        anchor.release(Deadline.after(1))

    assert raised.value is interrupted
    assert process.terminated
    assert raised.value.__notes__


@pytest.mark.parametrize("nonce", ["A" * 32, "a" * 31, "a" * 33, None])
def test_invalid_nonce_source_refuses_before_process_creation(nonce: object) -> None:
    process = FakeProcess([ready()])
    subject, host, _ = launcher(process, nonce=nonce)

    with pytest.raises(ValidationError):
        subject.start(Deadline.after(1))

    assert host.argv is None


def test_expired_deadline_refuses_before_process_creation() -> None:
    process = FakeProcess([ready()])
    subject, host, _ = launcher(process)

    with pytest.raises(ValidationError):
        subject.start(Deadline.after(0))

    assert host.argv is None


def test_import_does_not_load_legacy_wsl_or_transport_modules() -> None:
    import subprocess
    import sys

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
