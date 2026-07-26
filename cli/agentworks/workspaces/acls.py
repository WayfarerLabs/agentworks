"""The canonical workspace ACL, in one place.

``workspace create``, ``workspace repair``, and the VM init driver apply this
group ACL through the shared ``apply_workspace_acls`` helper below, so those
three cannot drift from each other. In particular a freshly created workspace
is already in repair's canonical state, so a first ``workspace repair`` is a
true no-op (``OK: ACLs`` / ``No issues found``) rather than reporting a spurious
fix.

Two other call sites, ``workspace copy`` and ``workspace rehome``, still apply
ACLs inline rather than through this helper, so the guarantee above does NOT
extend to them; folding them onto this spec is tracked as issue #263.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.transports import Transport


def apply_workspace_acls(target: Transport, path: str) -> None:
    """Apply the canonical group ACL to ``path`` and its whole subtree.

    Two applications, both recursive over the existing tree so PRE-EXISTING
    entries (e.g. an initial checkout placed at create time) are covered, not
    only entries created later:

    - a **default** ACL on every directory (``-d`` applies only to
      directories, so ``find`` avoids setfacl's warning on files), so entries
      created later inherit group ``rwx`` and a matching mask;
    - an **access** ACL over the whole tree, so entries that already exist are
      group ``rwx``.

    Recursive ``g::rwx`` only opens access to the owning group, never to
    ``other`` (which is deliberately left untouched here, see #254). At the
    per-workspace call sites the owning group is the workspace group that
    already owns the tree (mode ``2770`` + SGID), so this grants nothing new;
    at the VM-init call site the same helper runs over the shared workspaces
    parent, seeding the default ACL that per-workspace trees inherit.
    """
    quoted = shlex.quote(path)
    # Default ACLs on directories only; find avoids setfacl's warning on files.
    target.run(
        f"find {quoted} -type d -exec setfacl -d -m g::rwx -m m::rwx {{}} +",
        sudo=True,
        timeout=120,
    )
    # Access ACLs over every existing entry.
    target.run(f"setfacl -R -m g::rwx -m m::rwx {quoted}", sudo=True, timeout=120)
