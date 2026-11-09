"""Model the shell framing around canned native runtime observations."""

from __future__ import annotations

import copy
import re
import shlex
from typing import Protocol

_FRAME = re.compile(r"printf '\\nAGW_RUNTIME_([A-Z]+)='; (?:printf '\\nAGW_RUNTIME_STDERR=' >&2; )?")


def owner_probe_line(command: str) -> str:
    """The line a stale-socket probe runs as the socket's owner (``runuser -u O -- sh -c LINE``), else ``command``."""
    if "runuser -u " not in command:
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
