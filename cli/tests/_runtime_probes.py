"""Model the shell framing around canned native runtime observations."""

from __future__ import annotations

import copy
import re
import shlex
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agentworks.db import SessionRow
    from agentworks.sessions.manager import SessionTmuxServer

_FRAME = re.compile(r"printf '\\nAGW_RUNTIME_([A-Z]+)='; (?:printf '\\nAGW_RUNTIME_STDERR=' >&2; )?")


_SOCKET_GUARD = re.compile(
    r"\{ test ! -L (\S+) && \{ test ! -e \1 \|\| \{ test -S \1 && "
    r'test "\$\(stat -c %U -- \1\)" = (\S+); \}; \}; \} \|\| \{ (?:printf .*? >&2; )?exit 126; \}; (?P<line>.*)',
    re.DOTALL,
)


def socket_guard(command: str) -> tuple[str, str] | None:
    """The socket path and owner a ``tmux.as_socket_owner_line`` guard checks, or ``None`` for an unguarded line."""
    guarded = _SOCKET_GUARD.fullmatch(command)
    if guarded is None:
        return None
    return shlex.split(guarded[1])[0], shlex.split(guarded[2])[0]


# ``tmux.owner_client_command`` around ``/bin/sh -c``, as ``as_socket_owner_line`` switches to the
# owner: plain, or onto a relay terminal of its own.
_OWNER_SWITCH = re.compile(
    r"\(cd / && \S*runuser(?P<relay> --pty)? -u (?P<owner>\S+) -- /bin/sh -c (?P<line>.*)\)",
    re.DOTALL,
)


def owner_switch(command: str) -> tuple[str, bool] | None:
    """The owner a guarded line switches to and whether onto a relay terminal, or ``None`` for no switch."""
    guarded = _SOCKET_GUARD.fullmatch(command)
    switch = _OWNER_SWITCH.fullmatch(guarded["line"] if guarded is not None else command)
    if switch is None:
        return None
    return shlex.split(switch["owner"])[0], switch["relay"] is not None


def owner_probe_line(command: str) -> str:
    """The line a socket-owner request runs as the socket's owner, else ``command``.

    Unwraps ``tmux.as_socket_owner_line``'s guard and, when present, its
    switch to the owner (``owner_client_command`` around ``/bin/sh -c LINE``),
    as the stale-socket probe, every owner read and every attach send them.
    """
    guarded = _SOCKET_GUARD.fullmatch(command)
    if guarded is not None:
        command = guarded["line"]
    switch = _OWNER_SWITCH.fullmatch(command)
    if switch is None:
        return command
    return shlex.split(switch["line"])[0]


def runtime_probe_parts(command: str) -> tuple[str, str] | None:
    line = owner_probe_line(command)
    match = _FRAME.search(line)
    if match is None:
        return None
    return match.group(1), line[match.end() :]


class _NativeResult(Protocol):
    stdout: str
    stderr: str


def framed_runtime_result[T: _NativeResult](command: str, result: T) -> T:
    """Simulate printf without modifying a reusable canned native response."""
    parts = runtime_probe_parts(command)
    if parts is None:
        return result
    framed = copy.copy(result)
    framed.stdout = f"\nAGW_RUNTIME_{parts[0]}=" + result.stdout
    if "AGW_RUNTIME_STDERR=" in command:
        framed.stderr = "\nAGW_RUNTIME_STDERR=" + result.stderr
    return framed


def server_for(row: SessionRow, *, admin_username: str = "admin") -> SessionTmuxServer:
    """The tmux server ``resolve_session_tmux_server`` validates for a row its own agent or admin owns."""
    from agentworks.sessions.manager import SessionTmuxServer
    from agentworks.sessions.tmux import socket_owner

    assert row.socket_path is not None
    owner = socket_owner(row.socket_path, admin_username=admin_username, session_name=row.name)
    return SessionTmuxServer(row.socket_path, owner)
