"""Explicit local TCP listeners owned by one isolated foreground SSH client."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from threading import Event, Thread
from typing import TYPE_CHECKING

from agentworks.errors import ConnectivityError, StateError, ValidationError
from agentworks.execution.carrier import Failure, PreparedInvocation
from agentworks.execution.carriers.ssh._io import _child_environment, _cleanup
from agentworks.execution.carriers.ssh.client import check_client_version
from agentworks.execution.carriers.ssh.connection import admit_connection, build_ssh_argv

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType
    from typing import Self

    from agentworks.execution.carrier import Deadline
    from agentworks.execution.carriers.ssh.connection import SSHConnection

_POLL_SECONDS = 0.01
_CHUNK = 65_536
_JOIN_SECONDS = 1.0


@dataclass(frozen=True)
class LocalForward:
    """An adapter-author input: numeric bind address and literal destination.

    Separate IPv4 and IPv6 requests must each establish their own listener.
    Destination names are resolved by the server when a connection is forwarded.
    """

    bind_address: IPv4Address | IPv6Address
    local_port: int
    destination_host: str
    destination_port: int

    def __post_init__(self) -> None:
        if not isinstance(self.bind_address, IPv4Address | IPv6Address) or "%" in str(self.bind_address):
            raise ValidationError("Forwarding requires a numeric bind address without a scope")
        for port in (self.local_port, self.destination_port):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValidationError("Forwarding ports must be integers from 1 through 65535")
        if not isinstance(self.destination_host, str) or not re.fullmatch(
            r"[A-Za-z0-9_:][A-Za-z0-9_.:-]*", self.destination_host
        ):
            raise ValidationError("Forwarding destination must be a literal DNS name or unbracketed IP address")
        if ":" in self.destination_host:
            try:
                IPv6Address(self.destination_host)
            except ValueError:
                raise ValidationError("Forwarding destination IPv6 address must be unbracketed and unscoped") from None

    def _operand(self) -> str:
        destination = f"[{self.destination_host}]" if ":" in self.destination_host else self.destination_host
        return f"[{self.bind_address}]:{self.local_port}:{destination}:{self.destination_port}"


class ForwardingError(ConnectivityError):
    """Safe local forwarding evidence; raw client diagnostics are never retained."""

    def __init__(self, failure: Failure, local_status: int | None = None) -> None:
        super().__init__(f"SSH forwarding failed: {failure.value}")
        self.failure = failure
        self.local_status = local_status


class OwnedForwarding:
    """Own listeners, client and drain worker until close or natural client exit.

    Readiness proves local setup and an authenticated held session, not destination
    health or permission for a later forwarded connection. Call close or use a
    context manager even when wait is never called. Only the drain worker touches
    process pipes; callers may close while another caller waits.
    """

    def __init__(self, process: subprocess.Popen[bytes], marker: bytes) -> None:
        self._process = process
        self._marker = marker
        self._ready = Event()
        self._done = Event()
        self._stop = Event()
        self._failure: Failure | None = None
        self._status: int | None = None
        self._cleaned = False
        self._thread = Thread(target=self._drain, name="ssh-forwarding")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._close_preserving(exc)

    def close(self) -> None:
        """End owned stdin, then kill/reap locally within the cleanup allowance."""
        self._stop.set()
        join_until = time.monotonic() + _JOIN_SECONDS
        # The worker uses only nonblocking pipe reads, an interruptible 10ms
        # poll and the shared bounded kill/reap. Joining leaves no drainer alive.
        try:
            self._thread.join(timeout=_JOIN_SECONDS)
        except RuntimeError:
            # Thread.start can be interrupted before Python publishes its
            # started state, even when a native worker may still appear.
            # Stop is already set, so that worker cannot begin pipe reads.
            self._cleaned = _cleanup(self._process)
            if not self._done.wait(max(0.0, join_until - time.monotonic())):
                raise ForwardingError(Failure.OBSERVATION, self._process.returncode) from None
            self._thread.join(timeout=max(0.0, join_until - time.monotonic()))
        if self._thread.is_alive():
            raise ForwardingError(Failure.OBSERVATION, self._process.returncode)
        if not self._cleaned:
            raise ForwardingError(Failure.OBSERVATION, self._process.returncode)

    def _close_preserving(self, error: BaseException | None) -> None:
        try:
            self.close()
        except ForwardingError:
            if error is None:
                raise
            error.add_note("Local SSH forwarding cleanup did not complete within its bound.")

    def wait(self) -> int:
        """Return the local client's status; interruption closes and propagates."""
        try:
            while not self._done.wait(_POLL_SECONDS):
                pass
            self.close()
            if self._failure is not None:
                raise ForwardingError(self._failure, self._status)
            assert self._status is not None
            return self._status
        except BaseException as error:
            self._close_preserving(error)
            raise

    def _await_ready(self, deadline: Deadline) -> None:
        while True:
            if deadline.expired:
                raise ForwardingError(Failure.DEADLINE)
            if self._done.is_set():
                raise ForwardingError(self._failure or Failure.OBSERVATION, self._status)
            if self._ready.is_set():
                return
            remaining = deadline.remaining()
            self._done.wait(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))

    def _drain(self) -> None:
        received = bytearray()
        assert self._process.stdout is not None and self._process.stderr is not None
        outputs = [self._process.stdout, self._process.stderr]
        try:
            while not self._stop.is_set():
                self._status = self._process.poll()
                if self._status is not None:
                    return
                progressed = False
                for pipe in tuple(outputs):
                    try:
                        chunk = os.read(pipe.fileno(), _CHUNK)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        outputs.remove(pipe)
                        if pipe is self._process.stdout and not self._ready.is_set():
                            self._failure = Failure.INVALID_RESPONSE
                            return
                    progressed |= bool(chunk)
                    if pipe is self._process.stdout and not self._ready.is_set():
                        # Require the marker first, then discard later stdout.
                        # A pipe read may contain both; chunk boundaries must
                        # not change acceptance or increase retained bytes.
                        received.extend(chunk[: len(self._marker) - len(received)])
                        if not self._marker.startswith(received):
                            self._failure = Failure.INVALID_RESPONSE
                            return
                        if received == self._marker:
                            self._ready.set()
                            received.clear()
                if not progressed:
                    self._stop.wait(_POLL_SECONDS)
        except OSError:
            self._failure = Failure.OUTPUT
        except Exception:
            self._failure = Failure.OBSERVATION
        finally:
            try:
                self._cleaned = _cleanup(self._process)
            except OSError:
                self._cleaned = False
            if not self._cleaned:
                self._failure = Failure.OBSERVATION
            self._status = self._process.returncode
            self._done.set()


def open_local_forwards(
    connection: SSHConnection, forwards: Sequence[LocalForward], *, deadline: Deadline
) -> OwnedForwarding:
    """Open once and require a POSIX held-session acknowledgment before return.

    The startup deadline covers validation, client checking and authentication.
    It does not become a lifetime deadline for the returned resource. Accounts
    that prohibit command execution cannot establish this forwarding resource.
    """
    requests = tuple(forwards)
    if not requests or any(not isinstance(forward, LocalForward) for forward in requests):
        raise ValidationError("SSH forwarding requires at least one explicit local forward")
    if deadline.expired:
        raise ForwardingError(Failure.DEADLINE)
    try:
        trust = admit_connection(connection)
    except (OSError, StateError, ValidationError):
        raise ForwardingError(Failure.DISPATCH) from None
    if deadline.expired:
        raise ForwardingError(Failure.DEADLINE)
    failure = check_client_version(connection, deadline=deadline)
    if failure is not None:
        raise ForwardingError(failure)
    if deadline.expired:
        raise ForwardingError(Failure.DEADLINE)
    marker = f"agw-forward-ready-{secrets.token_hex(16)}"
    invocation = PreparedInvocation(("sh", "-c", f"printf '%s\\n' '{marker}'; IFS= read -r _; exit 0"))
    argv = build_ssh_argv(
        connection, invocation, trust=trust, local_forwards=tuple(forward._operand() for forward in requests)
    )
    if deadline.expired:
        raise ForwardingError(Failure.DEADLINE)
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=_child_environment(),
        )
    except (OSError, ValueError):
        raise ForwardingError(Failure.DISPATCH) from None
    resource: OwnedForwarding | None = None
    try:
        for pipe in (process.stdin, process.stdout, process.stderr):
            assert pipe is not None
            os.set_blocking(pipe.fileno(), False)
        resource = OwnedForwarding(process, (marker + "\n").encode("ascii"))
        # Retain ownership before starting: Thread.start itself can interrupt.
        resource._thread.start()
        resource._await_ready(deadline)
        return resource
    except BaseException as error:
        if resource is None:
            if not _cleanup(process):
                error.add_note("Local SSH forwarding cleanup did not complete within its bound.")
        else:
            resource._close_preserving(error)
        if isinstance(error, (OSError, RuntimeError)):
            raise ForwardingError(Failure.OBSERVATION, process.returncode) from None
        raise
