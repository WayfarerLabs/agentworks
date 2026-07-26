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
    """Apply the canonical workspace ACL to ``path`` and its whole subtree.

    Two applications, both recursive over the existing tree so PRE-EXISTING
    entries (e.g. an initial checkout placed at create time) are covered, not
    only entries created later:

    - a **default** ACL on every directory (``-d`` applies only to
      directories, so ``find`` avoids setfacl's warning on files), so entries
      created later inherit group ``rwx``, a matching mask, and ``other::---``;
    - an **access** ACL over the whole tree, so entries that already exist are
      group ``rwx`` with ``other::---``.

    The canonical spec opens access to the owning group (``g::rwx`` + a
    matching mask) and denies ``other`` (per #254). ``other`` is denied on
    both the default ACL (so new entries are not world-readable/traversable,
    the umask-027 hardening that entries created inside a workspace had been
    escaping via ``default:other::r-x``) and the recursive access ACL (so
    EXISTING entries created under the old default lose their ``other::r-x``,
    which otherwise travels with any file copied or moved out of the tree).
    Denying ``other`` traverse (no ``x``) on inner directories is safe: the
    workspace root is mode ``2770`` (``other`` cannot traverse into the tree
    at all) and group members reach every entry through ``g::rwx``, never
    through ``other``, so owner/group access is fully preserved.

    At the per-workspace call sites the owning group is the workspace group
    that already owns the tree (mode ``2770`` + SGID); at the VM-init call
    site the same helper runs over the shared workspaces parent, seeding the
    default ACL that per-workspace trees inherit.
    """
    quoted = shlex.quote(path)
    # Default ACLs on directories only; find avoids setfacl's warning on files.
    target.run(
        f"find {quoted} -type d -exec setfacl -d -m g::rwx -m m::rwx -m o::--- {{}} +",
        sudo=True,
        timeout=120,
    )
    # Access ACLs over every existing entry.
    target.run(f"setfacl -R -m g::rwx -m m::rwx -m o::--- {quoted}", sudo=True, timeout=120)
