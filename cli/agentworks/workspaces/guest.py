"""The guest requests more than one workspace operation sends, each built in one place.

A path or name reaches a script only as a positional parameter: :func:`script`
quotes each once into a ``set --`` line, so the source refers to it as ``"$1"``
and never interpolates it.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks.execution.errors import (
    CommandFailed,
    ExecutionIncomplete,
    FileConflict,
    FileLockTimeout,
    PathRefused,
    RequestRefused,
)
from agentworks.execution.requests import Command, Script, Shell

if TYPE_CHECKING:
    from agentworks.errors import AgentworksError

PROBE_DEADLINE = 10
"""Seconds a workspace path probe, link check, group removal or tmuxinator file operation may take."""

FIND_DEADLINE = 120
"""Seconds one recursive ``find`` or ``chown`` over a workspace tree may take."""

REQUEST_FAILURES: tuple[type[AgentworksError], ...] = (CommandFailed, ExecutionIncomplete, RequestRefused)
"""What a workspace step that tolerates a failed request catches: one that did not start, did not finish or failed.

A missing or mismatched host key, a VM that cannot run requests and an
elevated request on an agent's access are not among them: every later
request would fail the same way, so they propagate.
"""

FILE_FAILURES: tuple[type[AgentworksError], ...] = (*REQUEST_FAILURES, PathRefused, FileConflict, FileLockTimeout)
""":data:`REQUEST_FAILURES` and a refused or conflicting file operation, for a step that writes a file."""


def root(*argv: str, deadline: float | None = None) -> Command:
    """``argv`` run as root, a literal argument vector with no shell."""
    return Command(argv, elevated=True, deadline=deadline)


def script(source: str, *args: str, elevated: bool = False, deadline: float | None = None) -> Script:
    """``source`` run by ``/bin/sh`` with ``args`` as its positional parameters."""
    prefix = f"set -- {shlex.join(args)}\n" if args else ""
    return Script(prefix + source, Shell.SH, elevated=elevated, deadline=deadline)


def path_occupied(path: str) -> Script:
    """Exits 0 when anything, a dangling link included, is at ``path``."""
    return script('test -d "$1" || test -e "$1" || test -L "$1"', path, deadline=PROBE_DEADLINE)


def ensure_group(group: str) -> Script:
    """Creates the Linux group ``group`` unless it exists, as root."""
    return script('getent group "$1" >/dev/null 2>&1 || /usr/sbin/groupadd "$1"', group, elevated=True)


def open_parent_traversal(path: str) -> Script:
    """Adds ``a+x`` to every ancestor of ``path`` below ``/``, as root, printing a line for each it changed.

    The walk starts at the parent: the workspace's own mode is the canonical
    2770, which carries no world bits.
    """
    return script(
        'p=$(dirname "$1"); while [ "$p" != "/" ]; do chmod -c a+x "$p"; p=$(dirname "$p"); done',
        path,
        elevated=True,
    )
