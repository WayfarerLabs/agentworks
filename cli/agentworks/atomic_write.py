"""Atomic text-file replacement shared by the files agentworks writes for OpenSSH."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path


def atomic_write_text(
    path: Path,
    content: str,
    *,
    newline: str | None = None,
    errors: str = "strict",
    preserve_metadata: bool = False,
) -> None:
    """Write ``content`` to ``path`` through a temporary sibling and a rename.

    The temporary file is created by ``mkstemp``, so it is private (mode 0600)
    and the replacement keeps that mode, unless ``preserve_metadata`` is set and
    ``path`` exists: then the replacement takes the existing file's mode, and
    its owner and group where this process may set them. ``newline`` and
    ``errors`` are passed to ``open``: ``"\\n"`` keeps LF on Windows, which files
    OpenSSH parses want, and ``"surrogateescape"`` writes back bytes that were
    read undecodable.
    """
    existing = None
    if preserve_metadata:
        with contextlib.suppress(FileNotFoundError):
            existing = path.stat()
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        if existing is not None:
            if hasattr(os, "chown"):
                # Changing the owner, or the group to one the user is not in,
                # needs privileges; the mode still carries over.
                with contextlib.suppress(PermissionError):
                    os.chown(tmp, existing.st_uid, existing.st_gid)
            os.chmod(tmp, stat.S_IMODE(existing.st_mode))
        with open(fd, "w", encoding="utf-8", newline=newline, errors=errors) as f:
            f.write(content)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
