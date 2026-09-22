"""Private WSL2 guest-anchor lifecycle candidate.

This module deliberately reports only what its local boundaries observed. In
particular, closing a Windows Job Object or observing ``wsl.exe`` exit does not
establish that the Linux guest anchor is absent. A caller needs an independent
exact-identity observer to make that claim.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from agentworks.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution.carrier import Deadline
    from agentworks.execution.carriers.wsl2 import WSL2Connection

_MAX_RECEIPT_BYTES = 512
_NONCE = re.compile(r"[0-9a-f]{32}")
_READY = re.compile(r"READY ([0-9a-f]{32}) ([1-9][0-9]*) ([0-9]+)\n")
_EXITING = re.compile(r"(?:STOPPING|EXITING) ([0-9a-f]{32})\n")

# Python 3.11 syntax only. The proc start time is field 22, after the final
# right parenthesis because a process name may itself contain parentheses.
_HELPER_SOURCE = (
    "import os,sys;"
    "record=open('/proc/self/stat',encoding='ascii').read();"
    "tail=record[record.rfind(')')+2:].split();"
    "nonce=sys.argv[1];"
    "sys.stdout.write('READY {} {} {}\\n'.format(nonce,os.getpid(),tail[19]));"
    "sys.stdout.flush();"
    "sys.stdin.buffer.read();"
    "sys.stdout.write('EXITING {}\\n'.format(nonce));"
    "sys.stdout.flush()"
)


class JobAssignment(StrEnum):
    """What the launcher observed about assigning the host client to its job."""

    NOT_AVAILABLE = "not_available"
    NOT_ASSIGNED = "not_assigned"
    ASSIGNED = "assigned"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class HostClientStatus(StrEnum):
    """Host-client exit evidence, distinct from guest-anchor evidence."""

    NOT_OBSERVED = "not_observed"
    EXITED = "exited"
    UNKNOWN = "unknown"


class HelperReadiness(StrEnum):
    """Bounded helper READY receipt observation."""

    NOT_OBSERVED = "not_observed"
    READY = "ready"
    INVALID = "invalid"


class HelperExitReceipt(StrEnum):
    """Bounded helper receipt after controller EOF, not absence evidence."""

    NOT_OBSERVED = "not_observed"
    RECEIVED = "received"
    INVALID = "invalid"


class GuestAnchorPresence(StrEnum):
    """Result from an independent exact-identity guest observer."""

    PRESENT = "present"
    ABSENT_CONFIRMED = "absent_confirmed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GuestAnchorIdentity:
    """Guest PID and immutable Linux start-time identity from the helper."""

    pid: int
    start_time: int

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0 or type(self.start_time) is not int or self.start_time < 0:
            raise ValidationError("Guest anchor identity is invalid")


@dataclass(frozen=True)
class WSL2AnchorEvidence:
    """Separate facts observed while starting or releasing one guest anchor."""

    identity: GuestAnchorIdentity | None = None
    job_assignment: JobAssignment = JobAssignment.NOT_ASSIGNED
    pre_assignment_orphan_window: bool = False
    host_client_status: HostClientStatus = HostClientStatus.NOT_OBSERVED
    host_client_exit_status: int | None = None
    helper_readiness: HelperReadiness = HelperReadiness.NOT_OBSERVED
    helper_exit_receipt: HelperExitReceipt = HelperExitReceipt.NOT_OBSERVED
    guest_anchor_presence: GuestAnchorPresence = GuestAnchorPresence.UNKNOWN
    cleanup_failed: bool = False

    def __post_init__(self) -> None:
        if self.identity is None and self.helper_readiness == HelperReadiness.READY:
            raise ValidationError("Ready helper evidence requires a guest identity")
        if self.host_client_status != HostClientStatus.EXITED and self.host_client_exit_status is not None:
            raise ValidationError("Host exit status requires observed host exit")


class HostClient(Protocol):
    """Injected bounded local WSL-client process operations."""

    pid: int

    def read_stdout(self, limit: int, deadline: Deadline) -> bytes: ...

    def close_stdin(self) -> None: ...

    def wait(self, deadline: Deadline) -> int | None: ...

    def terminate(self, deadline: Deadline) -> None: ...


class HostClientBoundary(Protocol):
    """Injected host process creation boundary for this candidate."""

    def spawn(self, argv: tuple[str, ...]) -> HostClient: ...


class JobObject(Protocol):
    """Injected Job Object operations, each bounded by the caller deadline."""

    def assign(self, process: HostClient) -> None: ...

    def close(self, deadline: Deadline) -> None: ...


class JobObjectBoundary(Protocol):
    """Injected Job Object availability boundary."""

    def create(self) -> JobObject: ...


class GuestAnchorObserver(Protocol):
    """Externally proved observer for precisely one guest PID/start-time pair."""

    def observe(self, identity: GuestAnchorIdentity, deadline: Deadline) -> GuestAnchorPresence: ...


def _fresh_nonce() -> str:
    return secrets.token_hex(16)


def _valid_nonce(nonce: object) -> bool:
    return type(nonce) is str and _NONCE.fullmatch(nonce) is not None


def _ready_identity(receipt: object, nonce: str) -> GuestAnchorIdentity:
    if type(receipt) is not bytes or len(receipt) > _MAX_RECEIPT_BYTES:
        raise ValidationError("WSL2 helper readiness record is invalid")
    try:
        text = receipt.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValidationError("WSL2 helper readiness record is invalid") from error
    matched = _READY.fullmatch(text)
    if matched is None or matched.group(1) != nonce:
        raise ValidationError("WSL2 helper readiness record is invalid")
    return GuestAnchorIdentity(pid=int(matched.group(2)), start_time=int(matched.group(3)))


def _exit_receipt(receipt: object, nonce: str) -> HelperExitReceipt:
    if type(receipt) is not bytes or len(receipt) > _MAX_RECEIPT_BYTES:
        return HelperExitReceipt.INVALID
    if not receipt:
        return HelperExitReceipt.NOT_OBSERVED
    try:
        text = receipt.decode("ascii")
    except UnicodeDecodeError:
        return HelperExitReceipt.INVALID
    matched = _EXITING.fullmatch(text)
    return (
        HelperExitReceipt.RECEIVED if matched is not None and matched.group(1) == nonce else HelperExitReceipt.INVALID
    )


def _note(exception: BaseException, detail: str) -> None:
    exception.add_note(f"WSL2 guest-anchor cleanup: {detail}")


class WSL2GuestAnchor:
    """One started cooperative helper, with explicit release evidence."""

    def __init__(
        self,
        *,
        process: HostClient,
        job: JobObject | None,
        observer: GuestAnchorObserver | None,
        nonce: str,
        evidence: WSL2AnchorEvidence,
    ) -> None:
        self._process = process
        self._job = job
        self._observer = observer
        self._nonce = nonce
        self._evidence = evidence
        self._released = False

    @property
    def evidence(self) -> WSL2AnchorEvidence:
        """Return the latest immutable, bounded observation record."""
        return self._evidence

    def release(self, deadline: Deadline) -> WSL2AnchorEvidence:
        """Send EOF, collect bounded local facts, and never infer guest absence."""
        if self._released:
            return self._evidence
        try:
            self._release(deadline)
        except BaseException as error:
            self._emergency_cleanup(deadline, error)
            raise
        self._released = True
        return self._evidence

    def _release(self, deadline: Deadline) -> None:
        cleanup_failed = False
        try:
            self._process.close_stdin()
        except Exception:
            cleanup_failed = True

        try:
            receipt = self._process.read_stdout(_MAX_RECEIPT_BYTES + 1, deadline)
        except Exception:
            cleanup_failed = True
        else:
            self._evidence = replace(self._evidence, helper_exit_receipt=_exit_receipt(receipt, self._nonce))

        try:
            status = self._process.wait(deadline)
        except Exception:
            cleanup_failed = True
        else:
            if type(status) is int:
                self._evidence = replace(
                    self._evidence,
                    host_client_status=HostClientStatus.EXITED,
                    host_client_exit_status=status,
                )
            else:
                self._evidence = replace(self._evidence, host_client_status=HostClientStatus.UNKNOWN)

        if self._job is not None:
            try:
                self._job.close(deadline)
            except Exception:
                cleanup_failed = True

        self._observe(deadline)
        if cleanup_failed:
            self._evidence = replace(self._evidence, cleanup_failed=True)

    def _observe(self, deadline: Deadline) -> None:
        if self._observer is None or self._evidence.identity is None:
            return
        try:
            presence = self._observer.observe(self._evidence.identity, deadline)
        except Exception:
            self._evidence = replace(self._evidence, cleanup_failed=True)
            return
        if type(presence) is not GuestAnchorPresence:
            self._evidence = replace(self._evidence, cleanup_failed=True)
            return
        self._evidence = replace(self._evidence, guest_anchor_presence=presence)

    def _emergency_cleanup(self, deadline: Deadline, error: BaseException) -> None:
        cleanup_failed = False
        try:
            self._process.terminate(deadline)
        except BaseException:
            cleanup_failed = True
        try:
            self._process.wait(deadline)
        except BaseException:
            cleanup_failed = True
        if self._job is not None:
            try:
                self._job.close(deadline)
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            self._evidence = replace(self._evidence, cleanup_failed=True)
            _note(error, "was incomplete")


class WSL2GuestAnchorLauncher:
    """Build one literal WSL2 Python helper invocation through injected boundaries."""

    def __init__(
        self,
        connection: WSL2Connection,
        *,
        host: HostClientBoundary,
        jobs: JobObjectBoundary,
        observer: GuestAnchorObserver | None = None,
        nonce_factory: Callable[[], str] = _fresh_nonce,
    ) -> None:
        self._connection = connection
        self._host = host
        self._jobs = jobs
        self._observer = observer
        self._nonce_factory = nonce_factory

    def start(self, deadline: Deadline) -> WSL2GuestAnchor:
        """Spawn, attempt post-spawn job assignment, then require exact READY evidence."""
        if deadline.expired:
            raise ValidationError("WSL2 anchor start deadline has expired")
        nonce = self._nonce_factory()
        if not _valid_nonce(nonce):
            raise ValidationError("WSL2 anchor nonce source is invalid")
        job, assignment = self._job()
        process: HostClient | None = None
        evidence = WSL2AnchorEvidence(job_assignment=assignment)
        try:
            process = self._host.spawn(self._argv(nonce))
            if type(process.pid) is not int or process.pid <= 0:
                raise ValidationError("WSL2 host process identity is invalid")
            evidence = replace(evidence, pre_assignment_orphan_window=True)
            if job is not None:
                try:
                    job.assign(process)
                except Exception:
                    evidence = replace(evidence, job_assignment=JobAssignment.FAILED)
                except BaseException:
                    evidence = replace(evidence, job_assignment=JobAssignment.UNCERTAIN)
                    raise
                else:
                    evidence = replace(evidence, job_assignment=JobAssignment.ASSIGNED)
            identity = _ready_identity(process.read_stdout(_MAX_RECEIPT_BYTES + 1, deadline), nonce)
        except BaseException as error:
            if process is not None:
                if evidence.job_assignment == JobAssignment.UNCERTAIN:
                    _note(error, "job assignment state is uncertain")
                self._cleanup_failed_start(process, job, deadline, error)
            raise
        evidence = replace(evidence, identity=identity, helper_readiness=HelperReadiness.READY)
        return WSL2GuestAnchor(process=process, job=job, observer=self._observer, nonce=nonce, evidence=evidence)

    def _job(self) -> tuple[JobObject | None, JobAssignment]:
        try:
            return self._jobs.create(), JobAssignment.NOT_ASSIGNED
        except Exception:
            return None, JobAssignment.NOT_AVAILABLE

    def _argv(self, nonce: str) -> tuple[str, ...]:
        return (
            self._connection.wsl_executable,
            "--distribution",
            self._connection.distribution,
            "--user",
            self._connection.user,
            "--exec",
            "/usr/bin/python3",
            "-I",
            "-S",
            "-B",
            "-c",
            _HELPER_SOURCE,
            nonce,
        )

    @staticmethod
    def _cleanup_failed_start(
        process: HostClient, job: JobObject | None, deadline: Deadline, error: BaseException
    ) -> None:
        cleanup_failed = False
        try:
            process.terminate(deadline)
        except BaseException:
            cleanup_failed = True
        try:
            process.wait(deadline)
        except BaseException:
            cleanup_failed = True
        if job is not None:
            try:
                job.close(deadline)
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            _note(error, "was incomplete")
