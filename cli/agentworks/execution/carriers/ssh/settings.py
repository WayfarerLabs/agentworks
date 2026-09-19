"""Explicit operator settings for independent SSH delivery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agentworks.errors import ValidationError


def validate_literal_path(path: Path) -> str:
    """Validate a path supplied by config or an external connection caller.

    OpenSSH expands tokens after parsing quotes. Refuse that policy rather
    than turning a configured filename into another file or environment lookup.
    Validation does not access the filesystem.
    """
    value = path.as_posix()
    if (
        not path.is_absolute()
        or any(char in value for char in '%$"\\')
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValidationError("SSH paths must be absolute native paths without expansion tokens or control characters")
    return value


def validate_ssh_executable(value: str) -> None:
    """Validate an explicitly selected installed client at config/adapter boundaries."""
    if not isinstance(value, str) or not value:
        raise ValidationError("SSH executable must be a command name or absolute native path")
    if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
        return
    validate_literal_path(Path(value))


@dataclass(frozen=True)
class SSHSettings:
    """Loaded operator policy; endpoint selection belongs to composition.

    The config loader validates external TOML input. Missing files, installed
    client support and trust admission are checked by the operation using them.
    """

    trust_store: Path
    identity_file: Path
    agent_socket: str | None = None
    ssh_executable: str = "ssh"
    keepalive_interval: int = 15
    keepalive_count_max: int = 4
