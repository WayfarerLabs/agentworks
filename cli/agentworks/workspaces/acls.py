"""The canonical workspace ACL, in one place.

``workspace create``, ``workspace repair``, and the VM init driver all apply
the same group ACL to a workspace tree. Keeping the spec here means the three
can never drift, and in particular a freshly created workspace is already in
repair's canonical state, so a first ``workspace repair`` is a true no-op
(``OK: ACLs`` / ``No issues found``) rather than reporting a spurious fix.
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

    The workspace group already owns the tree (mode ``2770`` + SGID), so the
    recursive ``g::rwx`` grants nothing new; it just makes the ACL match the
    ownership. ``other`` is deliberately left untouched here (see #254).
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
