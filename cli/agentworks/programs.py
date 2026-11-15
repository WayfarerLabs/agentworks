"""Name a workstation program by its absolute path along ``PATH``, never by a bare name.

A bare program name is looked up when the process starts, and that lookup can
run a file from the current directory: Windows searches the current directory
before ``PATH``, and on POSIX an empty or relative ``PATH`` element means the
current directory. A program started through :func:`resolve_program`, which
considers only absolute ``PATH`` elements, is never one planted in whatever
directory the operator runs ``agw`` from. Every workstation program ``agw``
starts by name goes through it, and :func:`is_installed` answers whether one
is there by the same lookup. The OpenSSH client of the execution
transport has its own resolver (``execution.client``), which uses
:func:`find_program` on POSIX and fixed locations on Windows.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def find_program(name: str, *, search_path: str, windows: bool) -> Path | None:
    """The first executable ``name`` in an absolute element of ``search_path``, or ``None``.

    ``search_path`` is a ``PATH`` value in this platform's syntax. Empty and
    relative elements are skipped, since both name the current directory. On
    Windows the file is ``name.exe``, the one extension the system appends to a
    bare name, and an element may be quoted.
    """
    filename = f"{name}.exe" if windows and not name.lower().endswith(".exe") else name
    for element in search_path.split(os.pathsep):
        directory = element.strip('"') if windows else element
        if not directory or not os.path.isabs(directory):
            continue
        candidate = Path(directory, filename)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def resolve_program(name: str) -> str:
    """The absolute path of ``name`` along this process's ``PATH``, for an argv's first element.

    A name with a directory part (an operator's ``EDITOR=/usr/bin/vim``) is
    already a path, so it is returned unchanged, as a shell would run it.
    Raises ``FileNotFoundError`` when no absolute ``PATH`` element holds a
    bare name, as starting a missing program would, so callers keep one
    handler for both.
    """
    if os.path.dirname(name):
        return name
    found = find_program(name, search_path=os.environ.get("PATH", ""), windows=sys.platform == "win32")
    if found is None:
        raise FileNotFoundError(f"'{name}' was not found in an absolute PATH directory")
    return str(found)


def is_installed(name: str) -> bool:
    """Whether :func:`resolve_program` finds ``name``: the presence check that agrees with the start."""
    try:
        resolve_program(name)
    except FileNotFoundError:
        return False
    return True
