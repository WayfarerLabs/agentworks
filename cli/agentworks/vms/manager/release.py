"""Live Debian release observation and ordinary reconciliation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.debian import DebianRelease, probe_debian_release
from agentworks.errors import StateError

if TYPE_CHECKING:
    from agentworks.db import Database, VMRow
    from agentworks.transports import Transport


def verified_vm_release(db: Database, vm: VMRow, target: Transport) -> DebianRelease:
    """Return and persist a matching recognized live release.

    This ordinary service may fill an unknown row or refresh a matching
    observation. It deliberately refuses drift. ``vm upgrade`` owns the one
    adoption path for a guest that was upgraded outside Agentworks.
    """

    observed = probe_debian_release(target)
    if vm.debian_release is not None and vm.debian_release is not observed:
        raise StateError(
            f"VM release changed from recorded {vm.debian_release} to live {observed}",
            entity_kind="vm",
            entity_name=vm.name,
            hint=f"Run 'agw vm upgrade {vm.name}' to inspect and adopt an adjacent external upgrade.",
        )
    db.update_vm_debian_release(vm.name, observed)
    return observed
