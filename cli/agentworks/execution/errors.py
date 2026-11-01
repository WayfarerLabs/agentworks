"""Typed failures of the independent execution package.

Legacy modules may subclass these to join their own families (for example
``SSHTrustMissing`` is also an ``SSHError``); this module imports nothing from
them.
"""

from __future__ import annotations

from agentworks.errors import StateError


class TrustMissing(StateError):
    """A VM has no host-key record in its store, so nothing can be verified.

    Raised instead of falling back to first use: a missing record must never
    make an existing VM look new. Takes only the VM name because the message
    and the remediation are fixed by the type.
    """

    def __init__(self, vm_name: str) -> None:
        self.vm_name = vm_name
        super().__init__(
            f"VM '{vm_name}' has no recorded host key",
            entity_kind="vm",
            entity_name=vm_name,
            hint=(
                f"Run 'agw vm import-host-keys {vm_name}' to import it from ~/.ssh/known_hosts. "
                "If there is no entry to import, verify the VM's host key fingerprints against its "
                "console or provider, gather the keys with ssh-keyscan into a file, and pass it with "
                "--known-hosts."
            ),
        )
