"""Private ordinary-path observer for one exact WSL2 guest anchor.

The observer retains each native WSL client until local settlement is known.
It does not recover a client after controller death or establish native WSL proof.
"""

from __future__ import annotations

import secrets
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.execution._wsl2_guest_query import (
    FIXED_GUEST_QUERY_SOURCE,
    MAX_GUEST_QUERY_RESPONSE_BYTES,
    reduce_guest_query_response,
)
from agentworks.execution._wsl2_lifecycle import GuestAnchorIdentity, GuestAnchorPresence
from agentworks.execution.carrier import Deadline

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.execution._wsl2_lifecycle import OwnedHostClient
    from agentworks.execution.carriers.wsl2 import WSL2Connection

_CLEANUP_SECONDS = 0.5


class WSL2GuestObserver:
    """Run a fixed, no-staging query with a separately owned native client."""

    def __init__(
        self,
        connection: WSL2Connection,
        *,
        client_factory: Callable[[], OwnedHostClient] | None = None,
    ) -> None:
        from agentworks.execution._wsl2_windows import WindowsWSL2HostClient

        if connection.wsl_executable.casefold() not in {"wsl", "wsl.exe"}:
            raise ValidationError("WSL2 guest observation requires the native WSL executable")
        self._connection = connection
        self._client_factory = WindowsWSL2HostClient if client_factory is None else client_factory
        self._transition_lock = Lock()
        self._pending: OwnedHostClient | None = None

    def observe(self, identity: GuestAnchorIdentity, deadline: Deadline) -> GuestAnchorPresence:
        """Observe one identity; unresolved local custody prevents new dispatch."""
        if type(identity) is not GuestAnchorIdentity or type(deadline) is not Deadline or deadline.expires_at is None:
            raise ValidationError("WSL2 guest observation requires exact identity and finite deadline")
        remaining = deadline.remaining()
        assert remaining is not None
        if remaining <= 0 or not self._transition_lock.acquire(timeout=min(remaining, TIMEOUT_MAX)):
            return GuestAnchorPresence.UNKNOWN
        try:
            if not self._settle_pending() or deadline.expired:
                return GuestAnchorPresence.UNKNOWN
            nonce = secrets.token_hex(16)
            client = self._client_factory()
            self._pending = client  # Retain before any call that can dispatch.
            try:
                client.spawn_owned(self._argv(identity, nonce), deadline)
                client.close_stdin()
                response = client.read_stdout_line(MAX_GUEST_QUERY_RESPONSE_BYTES, deadline)
                exit_status = client.wait(deadline)
                trailing = client.read_stdout_line(1, deadline)
                complete = trailing == b""
                settled = self._settle_pending()
                if not settled:
                    return GuestAnchorPresence.UNKNOWN
                return reduce_guest_query_response(
                    response,
                    identity,
                    nonce,
                    exit_status=exit_status,
                    complete=complete,
                    deadline_expired=deadline.expired,
                )
            except BaseException as error:
                try:
                    self._settle_pending()
                except BaseException as cleanup_error:
                    if not isinstance(cleanup_error, Exception):
                        raise
                    error.add_note("WSL2 guest query local settlement remains uncertain")
                if isinstance(error, Exception):
                    return GuestAnchorPresence.UNKNOWN
                raise
        finally:
            self._transition_lock.release()

    def _settle_pending(self) -> bool:
        client = self._pending
        if client is None:
            return True
        try:
            settled = client.settle(Deadline.after(_CLEANUP_SECONDS)).settled
        except Exception:
            return False
        if settled:
            self._pending = None
        return settled

    def _argv(self, identity: GuestAnchorIdentity, nonce: str) -> tuple[str, ...]:
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
            FIXED_GUEST_QUERY_SOURCE,
            nonce,
            str(identity.pid),
        )
