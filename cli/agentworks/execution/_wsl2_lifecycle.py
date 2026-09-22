"""Private WSL2 guest-anchor lifecycle candidate.

This module reports independent local and guest observations. Closing a Windows
Job Object, observing ``wsl.exe`` exit, stdout EOF, and a helper EXITING record
do not establish that the Linux guest anchor is absent. Only an independent
exact-identity observer may report that fact.
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from agentworks.errors import ValidationError
from agentworks.execution.carrier import Deadline

if TYPE_CHECKING:
    from agentworks.execution.carriers.wsl2 import WSL2Connection

_CLEANUP_SECONDS = 0.5
_MAX_RECEIPT_BYTES = 512
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
    """Caller-visible outcome of post-spawn Job Object assignment.

    ``ASSIGNED_AFTER_SPAWN`` deliberately does not claim that controller death
    in the preceding spawn-to-assignment window could not orphan the guest.
    """

    NOT_AVAILABLE = "not_available"
    ASSIGNED_AFTER_SPAWN = "assigned_after_spawn"
    FAILED = "failed"


class HostClientStatus(StrEnum):
    """Host-client exit observation, distinct from guest-anchor observation."""

    NOT_OBSERVED = "not_observed"
    EXITED = "exited"
    UNKNOWN = "unknown"


class HostClientSettlement(StrEnum):
    """Whether this object retains a possibly live host-client capability."""

    ACTIVE = "active"
    EXIT_CONFIRMED = "exit_confirmed"
    UNCERTAIN = "uncertain"


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
    """Guest PID and Linux start-time identity parsed from an exact READY line."""

    pid: int
    start_time: int


@dataclass(frozen=True)
class WSL2AnchorEvidence:
    """Separate facts observed while starting or releasing one guest anchor."""

    identity: GuestAnchorIdentity
    job_assignment: JobAssignment
    host_client_status: HostClientStatus = HostClientStatus.NOT_OBSERVED
    host_client_exit_status: int | None = None
    host_client_settlement: HostClientSettlement = HostClientSettlement.ACTIVE
    helper_exit_receipt: HelperExitReceipt = HelperExitReceipt.NOT_OBSERVED
    guest_anchor_presence: GuestAnchorPresence = GuestAnchorPresence.UNKNOWN


class OwnedHostClient(Protocol):
    """Pre-created owner that retains host cleanup across spawn interruption.

    ``read_stdout_line`` returns one complete binary record terminated by a
    newline, EOF as ``b""``, or a record exceeding the requested bound. Native
    framing is deliberately left to a later adapter proof.
    """

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None: ...

    def read_stdout_line(self, limit: int, deadline: Deadline) -> bytes: ...

    def close_stdin(self) -> None: ...

    def wait(self, deadline: Deadline) -> int | None: ...

    def terminate(self, deadline: Deadline) -> None: ...


class JobObject(Protocol):
    """Injected Job Object operations, each bounded by the supplied deadline."""

    def assign(self, owner: OwnedHostClient, deadline: Deadline) -> None: ...

    def close(self, deadline: Deadline) -> None: ...


class GuestAnchorObserver(Protocol):
    """Externally proved observer for precisely one guest PID/start-time pair."""

    def observe(self, identity: GuestAnchorIdentity, deadline: Deadline) -> GuestAnchorPresence: ...


HostOwnerFactory = Callable[[], OwnedHostClient]
JobFactory = Callable[[Deadline], JobObject]


def _cleanup_deadline() -> Deadline:
    """Allocate the one fixed post-operation cleanup allowance."""
    return Deadline(time.monotonic() + _CLEANUP_SECONDS)


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


def _force_cleanup(
    owner: OwnedHostClient | None,
    job: JobObject | None,
    evidence: WSL2AnchorEvidence | None,
) -> tuple[WSL2AnchorEvidence | None, bool]:
    """Spend one fresh allowance forcing and observing locally owned resources."""
    cleanup = _cleanup_deadline()
    status: int | None = None
    if owner is not None:
        with suppress(BaseException):
            owner.terminate(cleanup)
        try:
            observed = owner.wait(cleanup)
        except BaseException:
            observed = None
        if type(observed) is int:
            status = observed
    if job is not None:
        with suppress(BaseException):
            job.close(cleanup)
    if evidence is None:
        return None, owner is None or status is not None
    if status is not None:
        return (
            replace(
                evidence,
                host_client_status=HostClientStatus.EXITED,
                host_client_exit_status=status,
                host_client_settlement=HostClientSettlement.EXIT_CONFIRMED,
            ),
            True,
        )
    return (
        replace(
            evidence,
            host_client_status=HostClientStatus.UNKNOWN,
            host_client_exit_status=None,
            host_client_settlement=HostClientSettlement.UNCERTAIN,
        ),
        False,
    )


class WSL2GuestAnchor:
    """One cooperative helper that retains host cleanup until exact local exit."""

    def __init__(
        self,
        *,
        owner: OwnedHostClient,
        job: JobObject | None,
        observer: GuestAnchorObserver | None,
        nonce: str,
        evidence: WSL2AnchorEvidence,
    ) -> None:
        self._owner = owner
        self._job = job
        self._observer = observer
        self._nonce = nonce
        self._evidence = evidence
        self._settled = False

    @property
    def evidence(self) -> WSL2AnchorEvidence:
        """Return the latest immutable, bounded observation record."""
        return self._evidence

    def release(self, deadline: Deadline) -> WSL2AnchorEvidence:
        """Request EOF, then retain cleanup capability until host exit is exact."""
        if self._settled:
            return self._evidence
        try:
            self._release(deadline)
        except BaseException as error:
            evidence, settled = _force_cleanup(self._owner, self._job, self._evidence)
            self._evidence = evidence or self._evidence
            if not settled:
                _note(error, "host-client settlement is uncertain")
            else:
                self._settled = True
            raise
        if self._evidence.host_client_settlement == HostClientSettlement.EXIT_CONFIRMED:
            self._settled = True
            self._observe(deadline)
        return self._evidence

    def _release(self, deadline: Deadline) -> None:
        if deadline.expired:
            evidence, _ = _force_cleanup(self._owner, self._job, self._evidence)
            self._evidence = evidence or self._evidence
            return
        try:
            self._owner.close_stdin()
        except Exception:
            evidence, _ = _force_cleanup(self._owner, self._job, self._evidence)
            self._evidence = evidence or self._evidence
            return
        if deadline.expired:
            evidence, _ = _force_cleanup(self._owner, self._job, self._evidence)
            self._evidence = evidence or self._evidence
            return
        try:
            receipt = self._owner.read_stdout_line(_MAX_RECEIPT_BYTES + 1, deadline)
        except Exception:
            receipt = None
        if receipt is not None:
            self._evidence = replace(self._evidence, helper_exit_receipt=_exit_receipt(receipt, self._nonce))
        if deadline.expired:
            evidence, _ = _force_cleanup(self._owner, self._job, self._evidence)
            self._evidence = evidence or self._evidence
            return
        try:
            status = self._owner.wait(deadline)
        except Exception:
            status = None
        if type(status) is int:
            if self._job is not None:
                with suppress(BaseException):
                    self._job.close(_cleanup_deadline() if deadline.expired else deadline)
            self._evidence = replace(
                self._evidence,
                host_client_status=HostClientStatus.EXITED,
                host_client_exit_status=status,
                host_client_settlement=HostClientSettlement.EXIT_CONFIRMED,
            )
            return
        evidence, _ = _force_cleanup(self._owner, self._job, self._evidence)
        self._evidence = evidence or self._evidence

    def _observe(self, deadline: Deadline) -> None:
        if self._observer is None or deadline.expired:
            return
        try:
            presence = self._observer.observe(self._evidence.identity, deadline)
        except Exception:
            return
        if type(presence) is GuestAnchorPresence:
            self._evidence = replace(self._evidence, guest_anchor_presence=presence)


class WSL2GuestAnchorLauncher:
    """Build one literal WSL2 Python helper invocation through injected boundaries."""

    def __init__(
        self,
        connection: WSL2Connection,
        *,
        owner_factory: HostOwnerFactory,
        job_factory: JobFactory,
        observer: GuestAnchorObserver | None = None,
    ) -> None:
        self._connection = connection
        self._owner_factory = owner_factory
        self._job_factory = job_factory
        self._observer = observer

    def start(self, deadline: Deadline) -> WSL2GuestAnchor:
        """Create an owner, dispatch once, and require an exact READY record."""
        if deadline.expired:
            raise ValidationError("WSL2 anchor start deadline has expired")
        job: JobObject | None = None
        assignment = JobAssignment.NOT_AVAILABLE
        owner: OwnedHostClient | None = None
        try:
            try:
                job = self._job_factory(deadline)
            except Exception:
                job = None
            if deadline.expired:
                raise ValidationError("WSL2 anchor start deadline has expired")
            owner = self._owner_factory()
            if deadline.expired:
                raise ValidationError("WSL2 anchor start deadline has expired")
            nonce = secrets.token_hex(16)
            owner.spawn_owned(self._argv(nonce), deadline)
            if deadline.expired:
                raise ValidationError("WSL2 anchor start deadline has expired")
            if job is not None:
                try:
                    job.assign(owner, deadline)
                except Exception:
                    assignment = JobAssignment.FAILED
                except BaseException as error:
                    _note(error, "job assignment state is uncertain")
                    raise
                else:
                    assignment = JobAssignment.ASSIGNED_AFTER_SPAWN
            if deadline.expired:
                raise ValidationError("WSL2 anchor start deadline has expired")
            identity = _ready_identity(owner.read_stdout_line(_MAX_RECEIPT_BYTES + 1, deadline), nonce)
        except BaseException as error:
            _, settled = _force_cleanup(owner, job, None)
            if owner is not None and not settled:
                _note(error, "host-client settlement is uncertain")
            raise
        evidence = WSL2AnchorEvidence(identity=identity, job_assignment=assignment)
        return WSL2GuestAnchor(owner=owner, job=job, observer=self._observer, nonce=nonce, evidence=evidence)

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
