"""Model the shell framing around canned native runtime observations."""

from __future__ import annotations

import copy
import re
import shlex
from typing import Protocol

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


# ``as_socket_owner_line``'s switch to the owner: plain, or onto a relay terminal of its own.
_OWNER_SWITCH = re.compile(r"(?P<relay>cd / && runuser --pty|runuser) -u (?P<owner>\S+) -- sh -c ")


def owner_switch(command: str) -> tuple[str, bool] | None:
    """The owner a guarded line switches to and whether onto a relay terminal, or ``None`` for no switch."""
    guarded = _SOCKET_GUARD.fullmatch(command)
    switch = _OWNER_SWITCH.match(guarded["line"] if guarded is not None else command)
    if switch is None:
        return None
    return shlex.split(switch["owner"])[0], switch["relay"] != "runuser"


def owner_probe_line(command: str) -> str:
    """The line a socket-owner request runs as the socket's owner, else ``command``.

    Unwraps ``tmux.as_socket_owner_line``'s guard and, when present, its
    switch to the owner (``runuser ... -- sh -c LINE``), as the stale-socket
    probe, every owner read and every attach send them.
    """
    guarded = _SOCKET_GUARD.fullmatch(command)
    if guarded is not None:
        command = guarded["line"]
    switch = _OWNER_SWITCH.match(command)
    if switch is None:
        return command
    return shlex.split(command[switch.end() :])[0]


def runtime_probe_parts(command: str) -> tuple[str, str] | None:
    match = _FRAME.search(owner_probe_line(command))
    if match is None:
        return None
    return match.group(1), command[match.end() :]


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
