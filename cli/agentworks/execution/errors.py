"""Typed failures of the independent execution package.

This module imports nothing from the legacy stack.
"""

from __future__ import annotations

from agentworks.errors import StateError


class TrustMissing(StateError):
    """A VM has no host-key record in its store, so nothing can be verified.

    Raised instead of falling back to first use: a missing record must never
    make an existing VM look new. It is a state error, not a connectivity one,
    so handlers that treat an unreachable peer as best-effort do not absorb it.
    Takes only the VM name because the message and the remediation are fixed by
    the type.
    """

    def __init__(self, vm_name: str) -> None:
        self.vm_name = vm_name
        super().__init__(
            f"VM '{vm_name}' has no recorded host key",
            entity_kind="vm",
            entity_name=vm_name,
            hint=(
                f"Run 'agw vm import-host-keys {vm_name}' (see its --help for --known-hosts), "
                "or delete and recreate a VM whose create never enrolled."
            ),
        )
