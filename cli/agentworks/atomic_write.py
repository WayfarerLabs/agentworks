"""Atomic text-file replacement shared by the files agentworks writes for OpenSSH."""

from __future__ import annotations

import contextlib
import os
import shlex
import stat
import tempfile
from pathlib import Path

from agentworks.errors import AuthorizationError
from agentworks.path_rendering import format_host_path


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
    group and mode. When this process may not give it that owner or group,
    ``path`` is left as it is and :class:`AuthorizationError` says how to make
    it the user's own. ``newline`` and ``errors`` mean what they mean to
    ``open``: ``"\\n"`` keeps LF on Windows, which files OpenSSH parses want, and
    ``"surrogateescape"`` writes back bytes that were read undecodable.
    """
    existing = None
    if preserve_metadata:
        with contextlib.suppress(FileNotFoundError):
            existing = path.stat()
    # Hidden, so ssh's Include of config.d/* never reads a temporary file left by a crash.
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    sibling = Path(name)
    try:
        with open(fd, "w", encoding="utf-8", newline=newline, errors=errors) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if existing is not None:
            _take_metadata(sibling, existing, path)
        sibling.replace(path)
    except BaseException:
        sibling.unlink(missing_ok=True)
        raise


def _take_metadata(sibling: Path, existing: os.stat_result, path: Path) -> None:
    """Give ``sibling`` the owner, group and mode in ``existing``, the metadata of ``path``.

    A refused owner or group raises :class:`AuthorizationError`, since a
    rename would hand ``path`` to this user with other permissions.
    """
    if hasattr(os, "chown"):
        try:
            os.chown(sibling, existing.st_uid, existing.st_gid)
        except PermissionError as error:
            raise AuthorizationError(
                f"cannot keep the owner and group of '{format_host_path(path)}' when replacing it",
                hint=f'Make it your own, then retry: chown "$USER": {shlex.quote(str(path))}',
            ) from error
    sibling.chmod(stat.S_IMODE(existing.st_mode))
