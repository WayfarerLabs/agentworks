"""The canonical workspace ACL, in one place.

Every workspace ACL application goes through the shared ``apply_workspace_acls``
helper below: ``workspace create``, ``workspace repair``, ``workspace copy``,
``workspace rehome``, and the VM init driver. They therefore cannot drift from
each other. In particular a freshly created, copied, or rehomed workspace is
already in repair's canonical state, so a first ``workspace repair`` is a true
no-op (``OK: ACLs`` / ``No issues found``) rather than reporting a spurious fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.execution.requests import Command

if TYPE_CHECKING:
    from agentworks.execution.files import Files

ACL_DEADLINE = 120
"""Seconds one recursive ACL pass over a workspace tree may take."""


def workspace_acl_commands(path: str) -> tuple[Command, ...]:
    """The three recursive ``setfacl`` passes over ``path``, as root, each a literal argv with no shell.

    VM initialization repairs the parent recursively while clones may still
    be running, so every pass prunes private clone attempts and never shares
    one or any of its descendants.
    """
    prune = ("find", path, "-type", "d", "-name", "*.agentworks-clone-*", "-prune", "-o")
    directory_or_executable = ("(", "-type", "d", "-o", "(", "-type", "f", "-perm", "/111", ")", ")")
    group_rwx, deny_other = ("-m", "g::rwx"), ("-m", "o::---", "{}", "+")
    passes = (
        ("-type", "d", "-exec", "setfacl", "-d", *group_rwx, "-m", "m::rwx", *deny_other),
        (*directory_or_executable, "-exec", "setfacl", *group_rwx, "-m", "m::rwx", *deny_other),
        ("-type", "f", "!", "-perm", "/111", "-exec", "setfacl", *group_rwx, "-m", "m::rw", *deny_other),
    )
    return tuple(Command((*prune, *rest), elevated=True, deadline=ACL_DEADLINE) for rest in passes)


def apply_workspace_acls(files: Files, path: str) -> None:
    """Share the tree with its owning group without inventing executable files.

    Directory defaults grant group rwx, with no other access. Regular files
    inherit that group entry but their creation mode masks out execution.
    Preserve this shape on existing files: the access mask is rw for ordinary
    files and rwx for directories and files already executable by any actor.
    This matches group-owned native publication and leaves its first repair
    unchanged. Inspect effective mode bits before modifying the ACL; setfacl's
    conditional X also sees masked inherited execute entries and would turn
    non-executable files into executable ones.

    Runs as root on ``files.access``; any pass that does not exit 0 raises.
    """
    for command in workspace_acl_commands(path):
        files.access.run(command, check=True)
