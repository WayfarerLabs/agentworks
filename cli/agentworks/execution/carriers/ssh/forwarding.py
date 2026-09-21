"""Explicit local TCP listeners owned by one isolated foreground SSH client."""

from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING

from agentworks.errors import ConnectivityError, StateError, ValidationError
from agentworks.execution._process import (
    LocalProcessOwner,
    LocalProcessPipes,
    LocalProcessRequest,
    LocalProcessTerminal,
    _retain_control_exception,
)
from agentworks.execution.carrier import Failure, PreparedInvocation
from agentworks.execution.carriers.ssh._io import _child_environment
from agentworks.execution.carriers.ssh.client import check_client_version
from agentworks.execution.carriers.ssh.connection import admit_connection, build_ssh_argv

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType
    from typing import IO, Self

    from agentworks.execution.carrier import Deadline
    from agentworks.execution.carriers.ssh.connection import SSHConnection

_POLL_SECONDS = 0.01
_CHUNK = 65_536
_JOIN_SECONDS = 1.0
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
    """Own listeners, client and drain worker until close or natural client exit.

    Readiness proves local setup and an authenticated held session, not destination
    health or permission for a later forwarded connection. Call close or use a
    context manager even when wait is never called. Only the drain worker touches
    process pipes; callers may close while another caller waits.
    """

    def __init__(self, owner: LocalProcessOwner, marker: bytes) -> None:
        self._owner = owner
        self._marker = marker
        self._ready = Event()
        self._done = Event()
        self._stop = Event()
        self._drain_admitted = Event()
        self._failure: Failure | None = None
        self._borrow_lock = Lock()
        self._close_lock = Lock()
        self._terminal: LocalProcessTerminal | None = None
        self._worker_start_attempted = False
        self._worker_stopped = False
        self._thread = Thread(target=self._drain, name="ssh-forwarding")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._close_preserving(exc)

    def close(self) -> None:
        """Stop pipe use, then settle the shared process owner exactly once."""
        self._settle(None)

    def _start(self, request: LocalProcessRequest) -> None:
        """Start the inert drainer, then admit it only after owner startup."""
        self._worker_start_attempted = True
        self._thread.start()
        self._owner.start(request)
        self._drain_admitted.set()

    def _close_preserving(self, error: BaseException | None) -> None:
        if error is None or not isinstance(error, Exception):
            self._settle(error)
            return
        try:
            self._settle(None)
        except ForwardingError:
            error.add_note("Local SSH forwarding cleanup did not complete within its bound.")

    def _settle(self, interruption: BaseException | None) -> LocalProcessTerminal:
        while True:
            try:
                if self._close_lock.acquire(timeout=_POLL_SECONDS):
                    break
            except BaseException as error:
                interruption = _retain_control_exception(interruption, error)
        try:
            worker_stopped, interruption = self._stop_worker(interruption)
            terminal = self._terminal
            if terminal is None:
                while True:
                    try:
                        terminal = self._owner.close()
                        break
                    except BaseException as error:
                        interruption = _retain_control_exception(interruption, error)
                        terminal = self._owner.snapshot().terminal
                        if terminal is not None:
                            break
                self._terminal = terminal
        finally:
            self._close_lock.release()

        cleanup_complete = worker_stopped and terminal is not None and terminal.cleaned
        if interruption is not None:
            if not cleanup_complete:
                interruption.add_note("Local SSH forwarding cleanup did not complete within its bound.")
            raise interruption
        if not cleanup_complete:
            raise ForwardingError(
                Failure.OBSERVATION,
                None if terminal is None else terminal.local_status,
            )
        assert terminal is not None
        if terminal.observation_failed:
            raise ForwardingError(Failure.OBSERVATION, terminal.local_status)
        return terminal

    def _stop_worker(self, interruption: BaseException | None) -> tuple[bool, BaseException | None]:
        while True:
            try:
                with self._borrow_lock:
                    self._stop.set()
                break
            except BaseException as error:
                interruption = _retain_control_exception(interruption, error)

        if not self._worker_start_attempted:
            self._worker_stopped = True
            return True, interruption
        if self._worker_stopped:
            return True, interruption

        wait_until = time.monotonic() + _JOIN_SECONDS
        while not self._done.is_set():
            remaining = wait_until - time.monotonic()
            if remaining <= 0:
                return False, interruption
            try:
                self._done.wait(min(_POLL_SECONDS, remaining))
            except BaseException as error:
                interruption = _retain_control_exception(interruption, error)
        while self._thread.is_alive():
            remaining = wait_until - time.monotonic()
            if remaining <= 0:
                return False, interruption
            try:
                self._thread.join(timeout=min(_POLL_SECONDS, remaining))
            except BaseException as error:
                interruption = _retain_control_exception(interruption, error)
        self._worker_stopped = True
        return True, interruption

    def wait(self) -> int:
        """Return the local client's status; interruption closes and propagates."""
        try:
            while not self._done.wait(_POLL_SECONDS):
                pass
            terminal = self._settle(None)
            if self._failure is not None:
                raise ForwardingError(self._failure, terminal.local_status)
            if terminal.exit_status is None:
                raise ForwardingError(Failure.OBSERVATION, terminal.local_status)
            return terminal.exit_status
        except BaseException as error:
            self._close_preserving(error)
            raise

    def _await_ready(self, deadline: Deadline) -> None:
        while True:
            if deadline.expired:
                raise ForwardingError(Failure.DEADLINE)
            if self._done.is_set():
                snapshot = self._owner.snapshot()
                local_status = snapshot.terminal.local_status if snapshot.terminal is not None else snapshot.exit_status
                raise ForwardingError(self._failure or Failure.OBSERVATION, local_status)
            if self._ready.is_set():
                return
            remaining = deadline.remaining()
            self._done.wait(_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining))

    def _configure_pipe(self, pipe: IO[bytes]) -> bool:
        with self._borrow_lock:
            if self._stop.is_set():
                return False
            os.set_blocking(pipe.fileno(), False)
            return True

    def _read(self, pipe: IO[bytes]) -> bytes | None:
        with self._borrow_lock:
            if self._stop.is_set():
                return None
            return os.read(pipe.fileno(), _CHUNK)

    def _drain_pipes(self, pipes: LocalProcessPipes) -> None:
        received = bytearray()
        outputs = [pipes.stdout, pipes.stderr]
        exit_seen_at: float | None = None
        for pipe in (pipes.stdin, *outputs):
            if pipe is not None:
                try:
                    configured = self._configure_pipe(pipe)
                except OSError:
                    self._failure = Failure.OBSERVATION
                    return
                if not configured:
                    return
        while not self._stop.is_set():
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
        environment = _child_environment()
        request = LocalProcessRequest(
            tuple(argv),
            True,
            None if environment is None else tuple(environment.items()),
        )
    except (OSError, ValueError):
        raise ForwardingError(Failure.DISPATCH) from None
    owner = LocalProcessOwner()
    resource = OwnedForwarding(owner, (marker + "\n").encode("ascii"))
    try:
        resource._start(request)
        del request
        resource._await_ready(deadline)
        return resource
    except BaseException as error:
        resource._close_preserving(error)
        if isinstance(error, (OSError, RuntimeError)):
            terminal = owner.snapshot().terminal
            raise ForwardingError(
                Failure.OBSERVATION,
                None if terminal is None else terminal.local_status,
            ) from None
        raise
