"""The canonical workspace ACL, in one place.

Every workspace ACL application goes through the shared ``apply_workspace_acls``
helper below: ``workspace create``, ``workspace repair``, ``workspace copy``,
``workspace rehome``, and the VM init driver. They therefore cannot drift from
each other. In particular a freshly created, copied, or rehomed workspace is
already in repair's canonical state, so a first ``workspace repair`` is a true
no-op (``OK: ACLs`` / ``No issues found``) rather than reporting a spurious fix.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.transports import Transport


def apply_workspace_acls(target: Transport, path: str) -> None:
    """Share the tree with its owning group without inventing executable files.

    Directory defaults grant group rwx, with no other access. Regular files
    inherit that group entry but their creation mode masks out execution.
    Preserve this shape on existing files: the access mask is rw for ordinary
    files and rwx for directories and files already executable by any actor.
    This matches group-owned native publication and leaves its first repair
    unchanged. Inspect effective mode bits before modifying the ACL; setfacl's
    conditional X also sees masked inherited execute entries and would turn
    non-executable files into executable ones.
    """
    quoted = shlex.quote(path)
    target.run(
        f"find {quoted} -type d -exec setfacl -d -m g::rwx -m m::rwx -m o::--- {{}} +",
        sudo=True,
        timeout=120,
    )
    target.run(
        rf"find {quoted} \( -type d -o \( -type f -perm /111 \) \) "
        "-exec setfacl -m g::rwx -m m::rwx -m o::--- {} +",
        sudo=True,
        timeout=120,
    )
    target.run(
        f"find {quoted} -type f ! -perm /111 -exec setfacl -m g::rwx -m m::rw -m o::--- {{}} +",
        sudo=True,
        timeout=120,
    )
