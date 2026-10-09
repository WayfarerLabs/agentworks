"""Explicit local TCP listeners owned by one isolated foreground SSH client."""

from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from threading import Event, Thread
from typing import TYPE_CHECKING

from agentworks.errors import ConnectivityError, StateError, ValidationError
from agentworks.execution._process import Deadline as ProcessDeadline
from agentworks.execution._process import (
    LocalProcessInput,
    LocalProcessOwner,
    LocalProcessPipes,
    LocalProcessRequest,
)
from agentworks.execution.carrier import Failure, PreparedInvocation
from agentworks.execution.carriers.ssh._io import _child_environment
from agentworks.execution.carriers.ssh.client import check_client_version, resolve_client_executable
from agentworks.execution.carriers.ssh.connection import admit_connection, build_ssh_argv

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import IO

    from agentworks.execution._delivery_custody import LocalDeliveryCustody
    from agentworks.execution.carrier import Deadline
    from agentworks.execution.carriers.ssh.connection import SSHConnection

_POLL_SECONDS = 0.01
_CHUNK = 65_536
_EXIT_DRAIN_SECONDS = 0.1


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
    """Caller-held forwarding session, including failed or interrupted startup.

    Construction is passive. Start and close are serialized by the caller;
    another caller may observe wait while close is requested. Close with a fresh
    finite deadline and retain this resource whenever settlement returns False.
    """

    def __init__(self, connection: SSHConnection, forwards: Sequence[LocalForward]) -> None:
        requests = tuple(forwards)
        if not requests or any(not isinstance(forward, LocalForward) for forward in requests):
            raise ValidationError("SSH forwarding requires at least one explicit local forward")
        self._connection = connection
        self._requests = requests
        self._custody: LocalDeliveryCustody | None = None
        self._owner: LocalProcessOwner | None = None
        self._marker = b""
        self._ready = Event()
        self._done = Event()
        self._stop = Event()
        self._drain_admitted = Event()
        self._failure: Failure | None = None
        self._started = False
        self._closed = False
        self._settled = False
        self._thread = Thread(target=self._drain, name="ssh-forwarding")

    def start(self, *, deadline: Deadline, custody: LocalDeliveryCustody) -> None:
        """Establish readiness once; failures leave the same resource caller-held."""
        if self._started or self._closed:
            raise StateError("SSH forwarding startup is no longer available")
        if deadline.expires_at is None:
            raise ValidationError("SSH forwarding startup requires a finite deadline")
        if not custody.settled:
            raise StateError("Local delivery custody is unsettled")
        self._started = True
        self._custody = custody
        if deadline.expired:
            raise ForwardingError(Failure.DEADLINE)
        try:
            trust = admit_connection(self._connection)
        except (OSError, StateError, ValidationError):
            raise ForwardingError(Failure.DISPATCH) from None
        if deadline.expired:
            raise ForwardingError(Failure.DEADLINE)
        try:
            executable = resolve_client_executable(self._connection)
        except (OSError, ValidationError):
            raise ForwardingError(Failure.DISPATCH) from None
        if deadline.expired:
            raise ForwardingError(Failure.DEADLINE)
        failure = check_client_version(executable, deadline=deadline, custody=custody)
        if failure is not None:
            raise ForwardingError(failure)
        if not custody.settled:
            raise ForwardingError(Failure.OBSERVATION)
        if deadline.expired:
            raise ForwardingError(Failure.DEADLINE)
        marker = f"agw-forward-ready-{secrets.token_hex(16)}"
        self._marker = (marker + "\n").encode("ascii")
        invocation = PreparedInvocation(("sh", "-c", f"printf '%s\\n' '{marker}'; IFS= read -r _; exit 0"))
        argv = build_ssh_argv(
            self._connection,
            invocation,
            trust=trust,
            executable=executable,
            local_forwards=tuple(forward._operand() for forward in self._requests),
        )
        if deadline.expired:
            raise ForwardingError(Failure.DEADLINE)
        try:
            environment = _child_environment()
            request = LocalProcessRequest(
                tuple(argv),
                LocalProcessInput.PIPE,
                None if environment is None else tuple(environment.items()),
            )
        except (OSError, ValueError):
            raise ForwardingError(Failure.DISPATCH) from None
        self._owner = custody.begin_process()
        self._start(request, deadline)
        self._await_ready(deadline)

    def _start(self, request: LocalProcessRequest, deadline: Deadline) -> None:
        """Keep the pipe worker inert until shared owner admission returns."""
        assert self._owner is not None
        try:
            self._thread.start()
        except (OSError, RuntimeError):
            raise ForwardingError(Failure.OBSERVATION) from None
        self._owner.start(request, close_deadline=ProcessDeadline(deadline.expires_at))
        self._drain_admitted.set()

    def close(self, deadline: Deadline) -> bool:
        """Stop and join pipe borrowers before retrying exact native custody."""
        if deadline.expires_at is None:
            raise ValidationError("SSH forwarding cleanup requires a finite deadline")
        self._closed = True
        if self._settled:
            return True
        self._stop.set()
        # A failed startup can leave a thread starting late, but it cannot pass
        # the admission gate. Only an admitted borrower needs joining before
        # native cleanup; the caller serializes startup and close.
        if self._drain_admitted.is_set():
            remaining = deadline.remaining()
            assert remaining is not None
            if not self._done.wait(remaining):
                return False
            remaining = deadline.remaining()
            assert remaining is not None
            self._thread.join(timeout=remaining)
            if self._thread.is_alive():
                return False
        self._settled = self._custody is None or self._custody.close(deadline)
        return self._settled

    def wait(self) -> int:
        """Observe natural exit without cleanup; interruption retains ownership."""
        if self._owner is None:
            raise StateError("SSH forwarding has not admitted a session")
        while not self._done.wait(_POLL_SECONDS):
            pass
        snapshot = self._owner.snapshot()
        terminal = snapshot.terminal
        status = snapshot.exit_status if terminal is None else terminal.exit_status
        local_status = snapshot.exit_status if terminal is None else terminal.local_status
        if self._failure is not None:
            raise ForwardingError(self._failure, local_status)
        if snapshot.observation_failed or status is None:
            raise ForwardingError(Failure.OBSERVATION, local_status)
        return status

    def _await_ready(self, deadline: Deadline) -> None:
        while True:
            if deadline.expired:
                raise ForwardingError(Failure.DEADLINE)
            if self._done.is_set():
                assert self._owner is not None
                snapshot = self._owner.snapshot()
                local_status = snapshot.terminal.local_status if snapshot.terminal is not None else snapshot.exit_status
                raise ForwardingError(self._failure or Failure.OBSERVATION, local_status)
            if self._ready.is_set():
                return
            remaining = deadline.remaining()
            self._done.wait(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))

    def _configure_pipe(self, pipe: IO[bytes]) -> bool:
        if self._stop.is_set():
            return False
        os.set_blocking(pipe.fileno(), False)
        return True

    def _read(self, pipe: IO[bytes]) -> bytes | None:
        if self._stop.is_set():
            return None
        return os.read(pipe.fileno(), _CHUNK)

    def _drain_pipes(self, pipes: LocalProcessPipes) -> None:
        received = bytearray()
        outputs = [pipes.stdout, pipes.stderr]
        exit_seen_at: float | None = None
        for pipe in outputs:
            try:
                configured = self._configure_pipe(pipe)
            except OSError:
                self._failure = Failure.OBSERVATION
                return
            if not configured:
                return
        while not self._stop.is_set():
            assert self._owner is not None
            snapshot = self._owner.snapshot()
            if snapshot.observation_failed:
                self._failure = Failure.OBSERVATION
                return
            progressed = False
            for pipe in tuple(outputs):
                try:
                    chunk = self._read(pipe)
                except BlockingIOError:
                    continue
                if chunk is None:
                    return
                if not chunk:
                    outputs.remove(pipe)
                    if pipe is pipes.stdout and not self._ready.is_set():
                        self._failure = Failure.INVALID_RESPONSE
                        return
                progressed |= bool(chunk)
                if pipe is pipes.stdout and not self._ready.is_set():
                    received.extend(chunk[: len(self._marker) - len(received)])
                    if not self._marker.startswith(received):
                        self._failure = Failure.INVALID_RESPONSE
                        return
                    if received == self._marker:
                        self._ready.set()
                        received.clear()
            if snapshot.exit_status is not None:
                if exit_seen_at is None:
                    exit_seen_at = time.monotonic()
                if not outputs:
                    if not self._ready.is_set():
                        self._failure = Failure.INVALID_RESPONSE
                    return
                if time.monotonic() - exit_seen_at >= _EXIT_DRAIN_SECONDS:
                    self._failure = Failure.OUTPUT if self._ready.is_set() else Failure.INVALID_RESPONSE
                    return
            if not progressed:
                self._stop.wait(_POLL_SECONDS)

    def _drain(self) -> None:
        try:
            while not self._drain_admitted.is_set():
                if self._stop.wait(_POLL_SECONDS):
                    return
            while not self._stop.is_set():
                assert self._owner is not None
                snapshot = self._owner.snapshot()
                if snapshot.observation_failed:
                    self._failure = Failure.OBSERVATION
                    return
                if snapshot.terminal is not None:
                    if snapshot.terminal.dispatch_failed:
                        self._failure = Failure.DISPATCH
                    elif not snapshot.terminal.cleaned or snapshot.terminal.observation_failed:
                        self._failure = Failure.OBSERVATION
                    elif not self._ready.is_set():
                        self._failure = Failure.INVALID_RESPONSE
                    return
                if snapshot.pipes is not None:
                    self._drain_pipes(snapshot.pipes)
                    return
                self._stop.wait(_POLL_SECONDS)
        except OSError:
            self._failure = Failure.OUTPUT
        except Exception:
            self._failure = Failure.OBSERVATION
        finally:
            self._done.set()
