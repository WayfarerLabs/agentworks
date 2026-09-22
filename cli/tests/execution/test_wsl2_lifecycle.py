"""Portable evidence tests for the private WSL2 guest-anchor candidate."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _wsl2_lifecycle as lifecycle
from agentworks.execution._wsl2_lifecycle import (
    _HELPER_SOURCE,
    GuestAnchorIdentity,
    GuestAnchorPresence,
    HelperExitReceipt,
    HostClientSettlement,
    HostClientStatus,
    JobAssignment,
    WSL2GuestAnchorLauncher,
)
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.wsl2 import WSL2Connection


@dataclass
class FakeOwner:
    lines: list[object]
    statuses: list[int | None] = field(default_factory=lambda: [0])
    spawn_interrupt: BaseException | None = None
    close_fails_without_eof: bool = False
    read_fails: bool = False
    spawned: bool = False
    closed_stdin: bool = False
    terminated: bool = False
    spawn_deadlines: list[Deadline] = field(default_factory=list)
    cleanup_deadlines: list[Deadline] = field(default_factory=list)
    argv: tuple[str, ...] = ()

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None:
        assert not deadline.expired
        self.spawned = True
        self.argv = argv
        self.spawn_deadlines.append(deadline)
        if self.spawn_interrupt is not None:
            raise self.spawn_interrupt

    def read_stdout_line(self, limit: int, deadline: Deadline) -> bytes:
        assert limit == 513
        assert not deadline.expired
        if self.read_fails:
            raise OSError
        value = self.lines.pop(0)
        return value  # type: ignore[return-value]

    def close_stdin(self) -> None:
        if self.close_fails_without_eof:
            raise OSError
        self.closed_stdin = True

    def wait(self, deadline: Deadline) -> int | None:
        if deadline not in self.spawn_deadlines:
            assert not deadline.expired
            self.cleanup_deadlines.append(deadline)
        return self.statuses.pop(0) if self.statuses else None

    def terminate(self, deadline: Deadline) -> None:
        assert not deadline.expired
        self.terminated = True
        self.cleanup_deadlines.append(deadline)


@dataclass
class FakeJob:
    assign_fails: bool = False
    assign_interrupt: BaseException | None = None
    close_fails: bool = False
    assigned: list[FakeOwner] = field(default_factory=list)
    deadlines: list[Deadline] = field(default_factory=list)
    closed: bool = False

    def assign(self, owner: FakeOwner, deadline: Deadline) -> None:
        assert not deadline.expired
        self.assigned.append(owner)
        self.deadlines.append(deadline)
        if self.assign_interrupt is not None:
            raise self.assign_interrupt
        if self.assign_fails:
            raise OSError

    def close(self, deadline: Deadline) -> None:
        assert not deadline.expired
        self.closed = True
        self.deadlines.append(deadline)
        if self.close_fails:
            raise OSError


@dataclass
class FakeJobs:
    job: FakeJob | None = field(default_factory=FakeJob)
    unavailable: bool = False
    deadlines: list[Deadline] = field(default_factory=list)

    def __call__(self, deadline: Deadline) -> FakeJob:
        assert not deadline.expired
        self.deadlines.append(deadline)
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
    owner: FakeOwner,
    jobs: FakeJobs | None = None,
    observer: FakeObserver | None = None,
) -> tuple[WSL2GuestAnchorLauncher, FakeJobs]:
    actual_jobs = jobs or FakeJobs()
    return (
        WSL2GuestAnchorLauncher(connection(), owner_factory=lambda: owner, job_factory=actual_jobs, observer=observer),
        actual_jobs,
    )


def token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle.secrets, "token_hex", lambda size: "a" * (size * 2))  # type: ignore[attr-defined]


def test_launch_uses_one_fixed_literal_python_helper_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    owner = FakeOwner([ready(), b"EXITING " + b"a" * 32 + b"\n"])
    subject, _ = launcher(owner)

    anchor = subject.start(Deadline.after(1))

    assert owner.argv[:7] == (
        r"C:\Windows\System32\wsl.exe",
        "--distribution",
        "test-distro",
        "--user",
        "test-user",
        "--exec",
        "/usr/bin/python3",
    )
    assert owner.argv[7:12] == ("-I", "-S", "-B", "-c", _HELPER_SOURCE)
    assert owner.argv[12] == "a" * 32
    assert anchor.evidence.job_assignment == JobAssignment.ASSIGNED_AFTER_SPAWN


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
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    line = process.stdout.readline(513)
    _, received_nonce, pid_text, start_text = line.decode("ascii").split()
    assert received_nonce == nonce
    pid = int(pid_text)
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    assert int(stat[stat.rfind(")") + 2 :].split()[19]) == int(start_text)
    assert process.poll() is None
    process.stdin.close()
    assert process.stdout.readline(513) == f"EXITING {nonce}\n".encode()
    assert process.wait(timeout=2) == 0
    assert process.stderr.read() == b""


def test_ready_identity_and_exit_receipt_are_not_guest_absence_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    owner = FakeOwner([ready(), b"EXITING " + b"a" * 32 + b"\n"])
    subject, _ = launcher(owner)
    anchor = subject.start(Deadline.after(1))

    evidence = anchor.release(Deadline.after(1))

    assert owner.closed_stdin
    assert evidence.identity == GuestAnchorIdentity(137, 8192)
    assert evidence.helper_exit_receipt == HelperExitReceipt.RECEIVED
    assert evidence.host_client_status == HostClientStatus.EXITED
    assert evidence.host_client_settlement == HostClientSettlement.EXIT_CONFIRMED
    assert evidence.guest_anchor_presence == GuestAnchorPresence.UNKNOWN


@pytest.mark.parametrize("presence", list(GuestAnchorPresence))
def test_only_injected_exact_identity_observer_supplies_guest_presence(
    monkeypatch: pytest.MonkeyPatch, presence: GuestAnchorPresence
) -> None:
    token(monkeypatch)
    owner = FakeOwner([ready(), b""])
    observer = FakeObserver(presence)
    subject, _ = launcher(owner, observer=observer)
    anchor = subject.start(Deadline.after(1))

    evidence = anchor.release(Deadline.after(1))

    assert observer.seen == [GuestAnchorIdentity(137, 8192)]
    assert evidence.guest_anchor_presence == presence


def test_job_unavailability_and_assignment_failure_remain_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    unavailable, _ = launcher(FakeOwner([ready(), b""]), jobs=FakeJobs(unavailable=True))
    unavailable_anchor = unavailable.start(Deadline.after(1))
    failed, _ = launcher(FakeOwner([ready(), b""]), jobs=FakeJobs(job=FakeJob(assign_fails=True)))
    failed_anchor = failed.start(Deadline.after(1))

    assert unavailable_anchor.evidence.job_assignment == JobAssignment.NOT_AVAILABLE
    assert failed_anchor.evidence.job_assignment == JobAssignment.FAILED


@pytest.mark.parametrize(
    "receipt",
    [b"READY " + b"b" * 32 + b" 137 8192\n", b"READY " + b"a" * 32 + b" nope 8192\n", b"x" * 513],
)
def test_malformed_foreign_and_oversized_ready_records_abort_with_cleanup(
    monkeypatch: pytest.MonkeyPatch, receipt: bytes
) -> None:
    token(monkeypatch)
    owner = FakeOwner([receipt])
    subject, jobs = launcher(owner)

    with pytest.raises(ValidationError):
        subject.start(Deadline.after(1))

    assert owner.terminated
    assert jobs.job is not None and jobs.job.closed


def test_spawn_interruption_after_os_creation_cleans_precreated_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    interrupted = KeyboardInterrupt()
    owner = FakeOwner([], spawn_interrupt=interrupted)
    subject, jobs = launcher(owner)

    with pytest.raises(KeyboardInterrupt) as raised:
        subject.start(Deadline.after(1))

    assert raised.value is interrupted
    assert owner.spawned and owner.terminated
    assert jobs.job is not None and jobs.job.closed


def test_assignment_interruption_preserves_original_exception_and_attempts_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token(monkeypatch)
    interrupted = KeyboardInterrupt()
    owner = FakeOwner([ready()])
    jobs = FakeJobs(job=FakeJob(assign_interrupt=interrupted))
    subject, _ = launcher(owner, jobs=jobs)

    with pytest.raises(KeyboardInterrupt) as raised:
        subject.start(Deadline.after(1))

    assert raised.value is interrupted
    assert owner.terminated
    assert jobs.job is not None and jobs.job.closed
    assert raised.value.__notes__


def test_close_stdin_failure_without_eof_forces_host_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    owner = FakeOwner([ready()], statuses=[0], close_fails_without_eof=True)
    subject, _ = launcher(owner)
    anchor = subject.start(Deadline.after(1))

    evidence = anchor.release(Deadline.after(1))

    assert not owner.closed_stdin
    assert owner.terminated
    assert owner.cleanup_deadlines
    assert all(not deadline.expired for deadline in owner.cleanup_deadlines)
    assert evidence.host_client_settlement == HostClientSettlement.EXIT_CONFIRMED


def test_cooperative_release_forces_cleanup_when_wait_is_not_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    owner = FakeOwner([ready(), b"EXITING " + b"a" * 32 + b"\n"], statuses=[None, 0])
    subject, _ = launcher(owner)
    anchor = subject.start(Deadline.after(1))

    evidence = anchor.release(Deadline.after(1))

    assert owner.closed_stdin and owner.terminated
    assert evidence.helper_exit_receipt == HelperExitReceipt.RECEIVED
    assert evidence.host_client_settlement == HostClientSettlement.EXIT_CONFIRMED


def test_expired_operation_budget_uses_fresh_cleanup_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    owner = FakeOwner([ready()], statuses=[0])
    subject, _ = launcher(owner)
    anchor = subject.start(Deadline.after(1))

    evidence = anchor.release(Deadline.after(0))

    assert owner.terminated
    assert owner.cleanup_deadlines
    assert all(not deadline.expired for deadline in owner.cleanup_deadlines)
    assert evidence.host_client_settlement == HostClientSettlement.EXIT_CONFIRMED


def test_uncertain_force_cleanup_retains_retryable_host_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    token(monkeypatch)
    owner = FakeOwner([ready()], statuses=[None, 0], close_fails_without_eof=True)
    subject, _ = launcher(owner)
    anchor = subject.start(Deadline.after(1))

    first = anchor.release(Deadline.after(0))
    second = anchor.release(Deadline.after(0))

    assert first.host_client_settlement == HostClientSettlement.UNCERTAIN
    assert second.host_client_settlement == HostClientSettlement.EXIT_CONFIRMED
    assert owner.terminated


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
