"""What the workspace operations share of their guest requests: deadlines, what a step catches, and scripts.

The scripts take a path or name only as a positional parameter: :func:`script`
quotes each once into a ``set --`` line, so the source refers to it as ``"$1"``
and never interpolates it. :func:`ensure_group` and :func:`remove_group_member`
are shared with the agents' workspace grants.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution.errors import (
    CommandFailed,
    ExecutionIncomplete,
    FileConflict,
    FileContentMismatch,
    FileLockTimeout,
    PathRefused,
    RequestRefused,
)
from agentworks.execution.files import guest_path, normalized_guest_path
from agentworks.execution.requests import Command, Script, Shell

if TYPE_CHECKING:
    from agentworks.execution.access import VMAccess

PROBE_DEADLINE = 10
"""Seconds a workspace path probe or group removal may take."""

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

FILE_WRITE_FAILURES = (*REQUEST_FAILURES, PathRefused, FileConflict, FileContentMismatch, FileLockTimeout)
"""What a step that tolerates a failed file write catches: a failed request, or a file operation that was refused,
conflicted, timed out or wrote nothing.

The host-key, readiness and elevation errors still propagate, as for ``REQUEST_FAILURES``.
"""


def stored_workspace_path(path: str) -> str:
    """A workspace row's path, normalized, refusing one that is not absolute.

    Boundary: the path is persisted state. The state migration anchors a
    relative path in the admin's home, but one starting with ``~`` stays
    relative, since which home it named is not recorded, so nothing is run
    against it. A ``..`` component is kept: a command given the path resolves
    it on the VM, but the file operations cannot address files under it
    (:func:`addressable_workspace_path`).
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


def addressable_workspace_path(path: str) -> str:
    """A workspace row's path as the file operations address files under it, refusing one they cannot.

    That is :func:`stored_workspace_path`'s path and refusal, then the file
    operations' own check (``execution.files.guest_path``). What it refuses
    in a normalized absolute path is a ``..`` component, which names a
    directory only once the VM resolves it, or a character that is not
    printable. An earlier release stored such a path as typed; rehome reads it
    as a source and stores a target the file operations accept.
    """
    normalized = stored_workspace_path(path)
    try:
        return guest_path(normalized)
    except StateError:
        raise ValidationError(
            f"the file operations cannot address workspace path {path!r}: "
            "it has a '..' component or a character that is not printable",
            hint="Move the workspace with 'agw workspace rehome', which stores a path they can address.",
        ) from None


def script(source: str, *args: str, elevated: bool = False, deadline: float | None = None) -> Script:
    """``source`` run by ``/bin/sh`` with ``args`` as its positional parameters."""
    prefix = f"set -- {shlex.join(args)}\n" if args else ""
    return Script(prefix + source, Shell.SH, elevated=elevated, deadline=deadline)


def path_occupied(path: str) -> Script:
    """Exits 0 when anything, a dangling link included, is at ``path``: what create and copy refuse to reuse."""
    return script('test -e "$1" || test -L "$1"', path, deadline=PROBE_DEADLINE)


def ensure_group(group: str) -> Command:
    """Creates the Linux group ``group`` unless it exists, as root.

    ``groupadd -f`` exits 0 when the group already exists, so the request
    has no check-then-act window: two operations that each ensure the same
    group both succeed whichever creates it.
    """
    return Command(("/usr/sbin/groupadd", "-f", group), elevated=True)


GROUP_MEMBER_ABSENT_STATUSES = frozenset({0, 6})
"""The ``usermod -r -G`` statuses after which the user is not in the group.

0 is a removal, or a user who was not a member; 6 (``E_NOTFOUND``) is a
user or a group that does not exist. Any other status, a timeout waiting
for the shadow lock included, leaves the membership as it was.
"""


def remove_group_member(access: VMAccess, user: str, group: str) -> None:
    """Removes ``user`` from the Linux group ``group`` as root, raising unless the membership is gone.

    ``usermod -r -G`` edits the group file under shadow's lock after reading
    it, so removals of different users from one group never restore each
    other's member. A status outside :data:`GROUP_MEMBER_ABSENT_STATUSES`
    raises ``CommandFailed``, its hint naming the command and where its
    output is; an outcome that is not a completion raises its typed error.
    """
    request = Command(("usermod", "-r", "-G", group, user), elevated=True)
    result = access.run(request)
    status = result.require_completed(access.vm_name)
    if status not in GROUP_MEMBER_ABSENT_STATUSES:
        error = CommandFailed(access.vm_name, status, result, streams_logged=request.log_streams)
        error.add_hint(
            f"'usermod -r -G {group} {user}' did not remove the membership; "
            f"run 'agw vm logs {access.vm_name}' for its output."
        )
        raise error


def open_parent_traversal(path: str) -> Script:
    """Adds ``a+x`` to every ancestor of ``path`` below ``/``, as root, printing a line for each it changed.

    The walk starts at the parent: the workspace's own mode is the canonical
    2770, which carries no world bits. Callers pass a normalized path, with
    no empty component. The walk takes only absolute paths, so a relative one
    stops it at once, and it never reaches ``/``. A path with a ``..``
    component exits 2 before any change, since its ancestors are known only
    once the VM resolves it. Each ancestor is cut from the path by parameter
    expansion, never captured from a command's output, which would drop
    trailing newlines from its name and so name another directory.
    """
    return script(
        'case /$1/ in */../*) echo "refusing a path with a .. component" >&2; exit 2 ;; esac\n'
        "p=${1%/*}\n"
        'while [ -n "$p" ] && [ "${p#/}" != "$p" ]; do chmod -c a+x -- "$p"; p=${p%/*}; done',
        path,
        elevated=True,
        deadline=TRAVERSAL_DEADLINE,
    )
