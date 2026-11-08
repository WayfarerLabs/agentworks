"""Model the shell framing around canned native runtime observations."""

from __future__ import annotations

import copy
import re
from typing import Protocol

_FRAME = re.compile(r"printf '\\nAGW_RUNTIME_([A-Z]+)='; (?:printf '\\nAGW_RUNTIME_STDERR=' >&2; )?")


def runtime_probe_parts(command: str) -> tuple[str, str] | None:
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
