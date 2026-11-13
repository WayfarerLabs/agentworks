"""What the workspace operations share of their guest requests: deadlines, what a step catches, and scripts.

The scripts take a path or name only as a positional parameter: :func:`script`
quotes each once into a ``set --`` line, so the source refers to it as ``"$1"``
and never interpolates it. :func:`ensure_group` is shared with the agents'
workspace grants.
"""

from __future__ import annotations

import shlex

from agentworks.errors import ValidationError
from agentworks.execution.errors import CommandFailed, ExecutionIncomplete, RequestRefused
from agentworks.execution.files import normalized_guest_path
from agentworks.execution.requests import Script, Shell

PROBE_DEADLINE = 10
"""Seconds a workspace path probe, link check, group removal or tmuxinator file operation may take."""

FIND_DEADLINE = 120
"""Seconds one recursive ``find`` or ``chown`` over a workspace tree may take."""

TRAVERSAL_DEADLINE = 30
"""Seconds the parent traversal walk may take."""

REQUEST_FAILURES = (CommandFailed, ExecutionIncomplete, RequestRefused)
"""What a workspace step that tolerates a failed request catches: one that did not start, did not finish or failed.

A missing or mismatched host key, a VM that cannot run requests and an
elevated request on an agent's access are not among them: every later
request would fail the same way, so they propagate.
"""


def stored_workspace_path(path: str) -> str:
    """A workspace row's path, normalized, refusing one that is not absolute.

    Boundary: the path is persisted state. The state migration anchors a
    relative path in the admin's home, but one starting with ``~`` stays
    relative, since which home it named is not recorded, so nothing is run
    against it.
    """
    normalized = normalized_guest_path(path)
    if not normalized.startswith("/"):
        raise ValidationError(
            f"workspace path {path!r} is not an absolute path on the VM",
            hint=(
                "Delete and recreate the workspace: which directory this stored path names is not recorded, "
                "so delete leaves it on the VM for you to check."
            ),
        )
    return normalized


def script(source: str, *args: str, elevated: bool = False, deadline: float | None = None) -> Script:
    """``source`` run by ``/bin/sh`` with ``args`` as its positional parameters."""
    prefix = f"set -- {shlex.join(args)}\n" if args else ""
    return Script(prefix + source, Shell.SH, elevated=elevated, deadline=deadline)


def path_occupied(path: str) -> Script:
    """Exits 0 when anything, a dangling link included, is at ``path``: what create and copy refuse to reuse."""
    return script('test -e "$1" || test -L "$1"', path, deadline=PROBE_DEADLINE)


def ensure_group(group: str) -> Script:
    """Creates the Linux group ``group`` unless it exists, as root."""
    return script('getent group "$1" >/dev/null 2>&1 || /usr/sbin/groupadd "$1"', group, elevated=True)


def open_parent_traversal(path: str) -> Script:
    """Adds ``a+x`` to every ancestor of ``path`` below ``/``, as root, printing a line for each it changed.

    The walk starts at the parent: the workspace's own mode is the canonical
    2770, which carries no world bits. It walks only absolute paths, so a
    relative one, whose ``dirname`` ends at ``.``, stops it at once.
    """
    return script(
        'p=$(dirname "$1")\n'
        'while [ "$p" != "/" ] && [ "${p#/}" != "$p" ]; do chmod -c a+x "$p"; p=$(dirname "$p"); done',
        path,
        elevated=True,
        deadline=TRAVERSAL_DEADLINE,
    )
