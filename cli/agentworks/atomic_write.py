"""Atomic text-file replacement for the files agentworks owns and OpenSSH reads."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str, *, newline: str | None = None) -> None:
    """Write ``content`` to ``path`` through a temporary sibling and a rename.

    The temporary file is created by ``mkstemp``, so it is private (mode 0600)
    and the replacement keeps that mode. ``newline`` is passed to ``open``:
    ``"\\n"`` keeps LF on Windows, which files OpenSSH parses want.
    """
    # Hidden, so ssh's Include of config.d/* never reads a temporary file left by a crash.
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    sibling = Path(name)
    try:
        with open(fd, "w", encoding="utf-8", newline=newline) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        sibling.replace(path)
    except BaseException:
        sibling.unlink(missing_ok=True)
        raise
