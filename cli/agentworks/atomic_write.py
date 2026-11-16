"""Atomic text-file replacement shared by the files agentworks writes for OpenSSH."""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from pathlib import Path

from agentworks.errors import StateError
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
    group and mode. When this process may not give it that owner or group, the
    existing file is rewritten in place instead, which keeps all three: the
    sibling first takes a synced copy of the original, which is put back if the
    rewrite fails and kept, named in a :class:`StateError`, if even that fails.
    ``newline`` and ``errors`` mean what they mean to ``open``: ``"\\n"`` keeps LF
    on Windows, which files OpenSSH parses want, and ``"surrogateescape"``
    writes back bytes that were read undecodable.
    """
    data = _encode(content, newline, errors)
    existing = None
    if preserve_metadata:
        with contextlib.suppress(FileNotFoundError):
            existing = path.stat()
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    sibling = Path(name)
    try:
        with open(fd, "wb") as f:
            replace = existing is None or _take_metadata(sibling, existing)
            f.write(data if replace else path.read_bytes())
            f.flush()
            os.fsync(f.fileno())
        if replace:
            sibling.replace(path)
            return
    except BaseException:
        sibling.unlink(missing_ok=True)
        raise
    _rewrite_in_place(path, data, backup=sibling)


def _encode(content: str, newline: str | None, errors: str) -> bytes:
    """``content`` as ``open(..., "w", encoding="utf-8", newline=newline, errors=errors)`` would write it."""
    translated = newline if newline is not None else os.linesep
    if translated not in ("", "\n"):
        content = content.replace("\n", translated)
    return content.encode("utf-8", errors)


def _take_metadata(sibling: Path, existing: os.stat_result) -> bool:
    """Give ``sibling`` the owner, group and mode in ``existing``; ``False`` when the owner or group is refused."""
    if hasattr(os, "chown"):
        try:
            os.chown(sibling, existing.st_uid, existing.st_gid)
        except PermissionError:
            return False
    sibling.chmod(stat.S_IMODE(existing.st_mode))
    return True


def _rewrite_in_place(path: Path, data: bytes, *, backup: Path) -> None:
    """Overwrite ``path`` with ``data``, restoring it from ``backup`` if that fails, then remove ``backup``.

    The file is opened once, without truncating, so a refused open changes
    nothing and is raised as it is; the rewrite and any restore go through
    that one descriptor.
    """
    try:
        fd = os.open(path, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    except BaseException:
        backup.unlink()
        raise
    try:
        try:
            _overwrite(fd, data)
        except BaseException as error:
            try:
                _overwrite(fd, backup.read_bytes())
            except BaseException:
                raise StateError(
                    f"could not rewrite '{format_host_path(path)}' or restore it",
                    hint=f"Its previous content is in '{format_host_path(backup)}'.",
                ) from error
            backup.unlink()
            raise
    finally:
        os.close(fd)
    backup.unlink()


def _overwrite(fd: int, data: bytes) -> None:
    """Replace the whole content of the file open at ``fd`` with ``data``, synced to disk."""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
    os.fsync(fd)
