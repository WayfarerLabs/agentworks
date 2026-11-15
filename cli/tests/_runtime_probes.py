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


def owner_probe_line(command: str) -> str:
    """The line a socket-owner request runs as the socket's owner, else ``command``.

    Unwraps ``tmux.as_socket_owner_line``'s guard and, when present, its
    ``runuser -u O -- sh -c LINE``, as the stale-socket probe and every
    owner read send them.
    """
    guarded = _SOCKET_GUARD.fullmatch(command)
    if guarded is not None:
        command = guarded["line"]
    if re.match(r"runuser -u \S+ -- sh -c ", command) is None:
        return command
    words = shlex.split(command)
    start = words.index("runuser")
    return words[words.index("-c", start) + 1]


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
