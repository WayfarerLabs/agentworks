"""The lookup walk: which directories and links the kernel consults to reach each stored workspace path.

Path locks are named from the guest's own resolution, never from workstation
normalization, which is unsafe across links and refuses the ``..`` stored
legacy paths keep. :func:`walk_paths` runs one read-only plain-stdlib program
on the path's own VM for several stored paths at once. It resolves each path
component by component, as the kernel does: starting at ``/``, it looks each
name up in the current physical directory and records that directory; a
``.`` stays; a ``..`` records the current directory, where it is looked up,
then moves to the parent; a symbolic link records its own entry as a followed
link, and its target's components replace it, from ``/`` for an absolute
target. A missing name ends the physical part and the remaining names are
joined lexically, so a target not yet created or a directory already gone
still resolves. More than :data:`LINK_LIMIT` expansions refuse, as the kernel
does with ``ELOOP``. ``/`` itself is never recorded: every path would hold it
and none could change it.

The walk names locks and nothing else; it builds no lock entity and changes
nothing on the VM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import StateError
from agentworks.execution.errors import CommandFailed
from agentworks.execution.requests import PythonProgram

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.execution.access import VMAccess

LINK_LIMIT = 40
"""The kernel's limit on symbolic link expansions in one lookup."""

EXIT_LINK_LIMIT = 3
"""The program's status when a path needs more than :data:`LINK_LIMIT` expansions; stdout holds that path's index."""

_WALK = r"""
import json, os, stat, sys

LINK_LIMIT, EXIT_LINK_LIMIT = 40, 3


class TooManyLinks(Exception):
    pass


def names(path):
    return [name for name in path.split('/') if name not in ('', '.')]


def walk(path, unlink_only):
    directories, links = [], []
    current, pending, expansions = '/', names(path), 0
    while pending:
        name = pending.pop(0)
        if current != '/' and current not in directories:
            directories.append(current)
        if name == '..':
            current = os.path.dirname(current)
            continue
        entry = current.rstrip('/') + '/' + name
        if unlink_only and not pending:
            return directories, links, entry
        try:
            mode = os.lstat(entry).st_mode
        except (FileNotFoundError, NotADirectoryError):
            return directories, links, os.path.normpath('/'.join([entry] + pending))
        if not stat.S_ISLNK(mode):
            current = entry
            continue
        expansions += 1
        if expansions > LINK_LIMIT:
            raise TooManyLinks
        if entry not in links:
            links.append(entry)
        target = os.readlink(entry)
        if target.startswith('/'):
            current = '/'
        pending = names(target) + pending
    return directories, links, current


request = json.loads(sys.argv[1])
records = []
for index, path in enumerate(request['paths']):
    try:
        directories, links, final = walk(path, request['unlink_only'])
    except TooManyLinks:
        sys.stdout.write(str(index))
        raise SystemExit(EXIT_LINK_LIMIT)
    records += ['D' + name for name in directories] + ['L' + name for name in links] + ['F' + final]
sys.stdout.buffer.write(b''.join(os.fsencode(record) + b'\0' for record in records))
"""


@dataclass(frozen=True)
class PathWalk:
    """One stored path as the guest's lookup reached it.

    ``directories`` are those a name was looked up in, ``/`` excluded, and
    ``links`` each followed link's own entry (its directory joined with its
    name), both in the order the walk first met them. ``final`` is the path
    reached, or in unlink-only mode the entry the delete removes.
    """

    path: str
    directories: tuple[str, ...]
    links: tuple[str, ...]
    final: str


def walk_paths(access: VMAccess, paths: Sequence[str], *, unlink_only: bool = False) -> tuple[PathWalk, ...]:
    """Walk each of ``paths`` on ``access``'s VM, as ``stored_workspace_path`` returns them, in one run.

    The program runs elevated, as the workspace operations whose locks it
    names do, so it reaches every directory they reach. ``unlink_only`` is
    workspace delete's walk: every component but the last is resolved and the
    last names the entry itself, never read, so a dangling or cyclic final
    link names one entry like any other. A final ``..`` names no entry and is
    walked in full. Raises ``StateError`` for a path that needs more than
    :data:`LINK_LIMIT` link expansions.
    """
    if not paths:
        return ()
    request = PythonProgram(
        _WALK,
        args=(json.dumps({"paths": list(paths), "unlink_only": unlink_only}),),
        elevated=True,
        deadline=30,
    )
    result = access.run(request)
    status = result.require_completed(access.vm_name)
    if status == EXIT_LINK_LIMIT:
        index = result.stdout.decode("ascii", "replace")
        if not index.isdecimal() or int(index) >= len(paths):
            raise StateError(f"the path walk on VM '{access.vm_name}' reported malformed output")
        refused = paths[int(index)]
        raise StateError(
            f"workspace path {refused!r} on VM '{access.vm_name}' needs more than {LINK_LIMIT} symbolic link "
            "expansions to resolve",
            entity_kind="vm",
            entity_name=access.vm_name,
            hint="Replace the link loop or chain on the VM with a shorter one, then retry.",
        )
    if status:
        raise CommandFailed(access.vm_name, status, result, streams_logged=request.log_streams)
    return _parse(access.vm_name, paths, result.stdout)


def _parse(vm_name: str, paths: Sequence[str], stdout: bytes) -> tuple[PathWalk, ...]:
    """The walks the program reported, one per path in order.

    Boundary: the output is an external process's.
    """
    malformed = StateError(f"the path walk on VM '{vm_name}' reported malformed output")
    try:
        records = stdout.decode("utf-8").split("\0")
    except UnicodeDecodeError:
        raise StateError(f"the path walk on VM '{vm_name}' reached a path that is not UTF-8") from None
    if records.pop() != "":
        raise malformed
    walks: list[PathWalk] = []
    directories: list[str] = []
    links: list[str] = []
    for record in records:
        kind, name = record[:1], record[1:]
        if not _normalized(name) or len(walks) == len(paths):
            raise malformed
        if kind == "D":
            directories.append(name)
        elif kind == "L":
            links.append(name)
        elif kind == "F":
            walks.append(PathWalk(paths[len(walks)], tuple(directories), tuple(links), name))
            directories, links = [], []
        else:
            raise malformed
    if len(walks) != len(paths) or directories or links:
        raise malformed
    return tuple(walks)


def _normalized(name: str) -> bool:
    """Whether ``name`` is an absolute path in normal form: ``/``, or components none empty, ``.`` or ``..``."""
    if name == "/":
        return True
    return name.startswith("/") and all(part not in {"", ".", ".."} for part in name[1:].split("/"))
