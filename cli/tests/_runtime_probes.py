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
# ``as_server_owner`` around ``/bin/bash -c``: the owner runs the quoted command from ``/``.
_SERVER_OWNER = re.compile(r"\(cd / && sudo -n \S*runuser -u (\S+) -- /bin/bash -c ((?:'[^']*'|\"'\")+)\)")


def server_owner_parts(command: str) -> tuple[str, str] | None:
    """Return ``(owner, inner command)`` for a command ``server_owner_run_command`` wrapped."""
    match = _SERVER_OWNER.search(command)
    if match is None:
        return None
    return shlex.split(match.group(1))[0], shlex.split(match.group(2))[0]


def runtime_probe_parts(command: str) -> tuple[str, str] | None:
    owned = server_owner_parts(command)
    if owned is not None:
        command = owned[1]
    match = _FRAME.search(command)
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


def server_for(row: SessionRow, *, admin_username: str = "admin") -> SessionTmuxServer:
    """The tmux server ``resolve_session_tmux_server`` validates for a row its own agent owns."""
    from agentworks.sessions.manager import SessionTmuxServer
    from agentworks.sessions.tmux import socket_owner

    assert row.socket_path is not None
    return SessionTmuxServer(row.socket_path, socket_owner(row.socket_path, admin_username=admin_username))
