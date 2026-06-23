"""Shared helpers for non-SSH transports.

The Lima / RemoteLima / WSL2 transports all hit the same problem: their
underlying CLI tools (``limactl shell``, ``wsl.exe``) don't expose
``SetEnv``-style env injection, so we embed env vars as scoped bash
assignments at the head of the payload.
"""

from __future__ import annotations

import shlex


def env_assignment_prefix(env: dict[str, str] | None) -> str:
    """Return ``K1=v1 K2=v2 `` (trailing space) for ``env`` or empty.

    Bash interprets a leading sequence of ``K=v`` assignments as per-command
    env (exported to the command's process and any children it spawns).
    Values are quoted via :func:`shlex.quote` so spaces and shell
    metacharacters are safe.
    """
    if not env:
        return ""
    return "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
