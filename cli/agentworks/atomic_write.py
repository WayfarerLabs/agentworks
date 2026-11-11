"""Atomic text-file replacement shared by the files agentworks writes for OpenSSH."""

from __future__ import annotations

import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str, *, newline: str | None = None) -> None:
    """Write ``content`` to ``path`` through a temporary sibling and a rename.

    The temporary file is created by ``mkstemp``, so it is private (mode 0600)
    and the replacement keeps that mode. ``newline`` is passed to ``open``:
    ``"\\n"`` keeps LF on Windows, which files OpenSSH parses want.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8", newline=newline) as f:
            f.write(content)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
