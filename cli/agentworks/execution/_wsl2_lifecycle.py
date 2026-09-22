"""Private WSL2 guest-anchor lifecycle candidate.

Host-client, Job Object, helper, and guest observations are deliberately kept
separate. Neither local cleanup nor a helper receipt establishes that a Linux
guest anchor is absent. Only an independent exact-identity observer may report
that fact.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
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


class HostClientStatus(StrEnum):
    """What the native owner observed about its WSL client process."""

    NOT_CREATED = "not_created"
    ACTIVE = "active"
    EXITED = "exited"
    UNKNOWN = "unknown"


class JobAssignment(StrEnum):
    """What the native owner observed while assigning its Job Object.

    ``ASSIGNED_AFTER_SPAWN`` records successful post-spawn assignment. It does
    not claim that controller death before assignment could not orphan a guest.
    """

    NOT_CREATED = "not_created"
    ASSIGNED_AFTER_SPAWN = "assigned_after_spawn"
    FAILED = "failed"
    UNKNOWN = "unknown"


class JobHandleSettlement(StrEnum):
    """Whether the Job Object handle has been exactly settled locally."""

    NOT_CREATED = "not_created"
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class HelperExitReceipt(StrEnum):
    """Bounded helper receipt after EOF, not guest-absence evidence."""

    NOT_OBSERVED = "not_observed"
    RECEIVED = "received"
    INVALID = "invalid"


class GuestAnchorPresence(StrEnum):
    """Result from an independent exact-identity guest observer."""

    PRESENT = "present"
    ABSENT_CONFIRMED = "absent_confirmed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LocalResourceSnapshot:
    """Immutable native-owner facts for local process and Job Object resources."""

    host_client_status: HostClientStatus
    host_client_exit_status: int | None
    job_assignment: JobAssignment
    job_handle_settlement: JobHandleSettlement

    @property
    def settled(self) -> bool:
        """Whether both local resources are exactly settled or never created."""
        return self.host_client_status in {HostClientStatus.NOT_CREATED, HostClientStatus.EXITED} and (
            self.job_handle_settlement in {JobHandleSettlement.NOT_CREATED, JobHandleSettlement.CLOSED}
        )


@dataclass(frozen=True)
class GuestAnchorIdentity:
    """Guest PID and Linux start-time identity from an exact READY record."""

    pid: int
    start_time: int


@dataclass(frozen=True)
class WSL2AnchorEvidence:
    """Current independent local, helper, and guest observations."""

    local: LocalResourceSnapshot
    identity: GuestAnchorIdentity | None = None
    helper_exit_receipt: HelperExitReceipt = HelperExitReceipt.NOT_OBSERVED
    guest_anchor_presence: GuestAnchorPresence = GuestAnchorPresence.UNKNOWN


class OwnedHostClient(Protocol):
    """Inert native owner that retains every local capability through failure.

    Construction performs no native work. ``spawn_owned`` must publish any host
    process and Job Object handle into this object before an interruption can
    escape. ``read_stdout_line`` returns one complete bounded binary line, EOF
    as ``b""``, or a line exceeding the supplied bound. ``settle`` attempts all
    owned local cleanup before raising and keeps retryable snapshot state.
    """

    def spawn_owned(self, argv: tuple[str, ...], deadline: Deadline) -> None: ...

    def read_stdout_line(self, limit: int, deadline: Deadline) -> bytes: ...

    def close_stdin(self) -> None: ...

    def wait(self, deadline: Deadline) -> int | None: ...

    def snapshot(self) -> LocalResourceSnapshot: ...

    def settle(self, deadline: Deadline) -> LocalResourceSnapshot: ...


class GuestAnchorObserver(Protocol):
    """Externally proved observer for one precise guest PID/start-time pair."""

    def observe(self, identity: GuestAnchorIdentity, deadline: Deadline) -> GuestAnchorPresence: ...


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


def _note(primary: BaseException, detail: str) -> None:
    primary.add_note(f"WSL2 guest-anchor cleanup: {detail}")


class WSL2GuestAnchorOwner:
    """Caller-owned lifecycle around one inert native host-client owner.

    The caller constructs this object before dispatch and retains it after every
    start or release failure. This module intentionally supplies no native
    Windows adapter and does not claim live WSL proof.
    """

    def __init__(
        self,
        connection: WSL2Connection,
        native: OwnedHostClient,
        *,
        observer: GuestAnchorObserver | None = None,
    ) -> None:
        self._connection = connection
        self._native = native
        self._observer = observer
        self._local = native.snapshot()
        self._identity: GuestAnchorIdentity | None = None
        self._nonce: str | None = None
        self._helper_exit_receipt = HelperExitReceipt.NOT_OBSERVED
        self._guest_anchor_presence = GuestAnchorPresence.UNKNOWN
        self._start_attempted = False

    @property
    def evidence(self) -> WSL2AnchorEvidence:
        """Return the current immutable observation record without new I/O."""
        return WSL2AnchorEvidence(
            local=self._local,
            identity=self._identity,
            helper_exit_receipt=self._helper_exit_receipt,
            guest_anchor_presence=self._guest_anchor_presence,
        )

    def start(self, deadline: Deadline) -> WSL2AnchorEvidence:
        """Dispatch one helper while retaining this object through every outcome."""
        if self._start_attempted:
            raise ValidationError("WSL2 guest anchor was already started")
        if deadline.expired:
            raise ValidationError("WSL2 anchor start deadline has expired")
        self._nonce = secrets.token_hex(16)
        self._start_attempted = True
        try:
            self._native.spawn_owned(self._argv(self._nonce), deadline)
            self._refresh()
            if deadline.expired:
                raise ValidationError("WSL2 anchor start deadline has expired")
            receipt = self._native.read_stdout_line(_MAX_RECEIPT_BYTES + 1, deadline)
            identity = _ready_identity(receipt, self._nonce)
            self._identity = identity
            self._refresh()
            if deadline.expired:
                raise ValidationError("WSL2 anchor start deadline has expired")
        except BaseException as error:
            self._settle_after_failure(error)
            raise
        return self.evidence

    def release(self, deadline: Deadline) -> WSL2AnchorEvidence:
        """Observe cooperative EOF, settle local handles, then retry guest proof."""
        try:
            if not self._local.settled:
                self._cooperative_release(deadline)
                self._settle()
            self._observe_guest(deadline)
        except BaseException as error:
            self._settle_after_failure(error)
            raise
        return self.evidence

    def settle(self) -> WSL2AnchorEvidence:
        """Spend a fresh local cleanup allowance without a guest observation."""
        self._settle()
        return self.evidence

    def _cooperative_release(self, deadline: Deadline) -> None:
        if deadline.expired:
            return
        try:
            self._native.close_stdin()
        except Exception:
            return
        if deadline.expired:
            return
        try:
            receipt = self._native.read_stdout_line(_MAX_RECEIPT_BYTES + 1, deadline)
        except Exception:
            receipt = None
        if receipt is not None and self._nonce is not None:
            self._helper_exit_receipt = _exit_receipt(receipt, self._nonce)
        if deadline.expired:
            return
        try:
            self._native.wait(deadline)
        except Exception:
            return
        self._refresh()

    def _settle(self) -> None:
        self._local = self._native.settle(Deadline.after(_CLEANUP_SECONDS))

    def _settle_after_failure(self, primary: BaseException) -> None:
        try:
            self._settle()
        except BaseException:
            _note(primary, "native settlement raised")
            try:
                self._refresh()
            except BaseException:
                _note(primary, "native snapshot is unavailable")

    def _refresh(self) -> None:
        self._local = self._native.snapshot()

    def _observe_guest(self, deadline: Deadline) -> None:
        if (
            not self._local.settled
            or self._identity is None
            or self._observer is None
            or self._guest_anchor_presence == GuestAnchorPresence.ABSENT_CONFIRMED
            or deadline.expired
        ):
            return
        presence = self._observer.observe(self._identity, deadline)
        if type(presence) is GuestAnchorPresence:
            self._guest_anchor_presence = presence

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
