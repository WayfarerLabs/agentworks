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
    ``path`` exists: then the replacement takes the existing file's owner,
    group and mode. When this process may not give it that owner or group, the
    existing file is rewritten in place instead, which keeps all three but is
    not atomic. ``newline`` and ``errors`` are passed to ``open``: ``"\\n"`` keeps
    LF on Windows, which files OpenSSH parses want, and ``"surrogateescape"``
    writes back bytes that were read undecodable.
    """
    existing = None
    if preserve_metadata:
        with contextlib.suppress(FileNotFoundError):
            existing = path.stat()
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    replace = True
    try:
        with open(fd, "w", encoding="utf-8", newline=newline, errors=errors) as f:
            if existing is not None:
                replace = _take_metadata(Path(tmp), existing)
            if replace:
                f.write(content)
        if replace:
            Path(tmp).replace(path)
            return
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    Path(tmp).unlink()
    with open(path, "w", encoding="utf-8", newline=newline, errors=errors) as f:
        f.write(content)


def _take_metadata(tmp: Path, existing: os.stat_result) -> bool:
    """Give ``tmp`` the owner, group and mode in ``existing``; ``False`` when the owner or group is refused."""
    if hasattr(os, "chown"):
        try:
            os.chown(tmp, existing.st_uid, existing.st_gid)
        except PermissionError:
            return False
    tmp.chmod(stat.S_IMODE(existing.st_mode))
    return True
