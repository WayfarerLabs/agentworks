"""Live Debian release observation and ordinary reconciliation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import InitStatus
from agentworks.debian import DebianRelease, probe_debian_release
from agentworks.errors import StateError, UserAbort

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.transports import Transport


def verified_vm_release(db: Database, vm: VMRow, target: Transport) -> DebianRelease:
    """Return and persist a matching recognized live release.

    This ordinary service may fill an unknown row or refresh a matching
    observation. It deliberately refuses drift. ``vm confirm-release`` owns
    explicit adoption when an operator changed the guest outside Agentworks.
    """

    observed = probe_debian_release(target)
    if vm.debian_release is not None and vm.debian_release is not observed:
        raise StateError(
            f"VM release changed from recorded {vm.debian_release} to live {observed}",
            entity_kind="vm",
            entity_name=vm.name,
            hint=f"Run 'agw vm confirm-release {vm.name}' to inspect and adopt the live release.",
        )
    db.update_vm_debian_release(vm.name, observed)
    return observed


def confirm_vm_release(
    db: Database,
    config: Config,
    name: str,
    *,
    yes: bool,
    interaction: TtyInteractionPolicy,
) -> DebianRelease:
    """Observe and explicitly adopt a recognized live Debian release."""

    from agentworks.bootstrap import load_request_registry
    from agentworks.transports import transport

    from ._helpers import _require_vm
    from .boundary import gated_vm_boundary

    vm = _require_vm(db, name)
    registry = load_request_registry(config, live_database=db)
    with gated_vm_boundary(db, config, registry, vm, interaction=interaction):
        observed = probe_debian_release(transport(vm, config))

    recorded = vm.debian_release
    output.info(f"Recorded Debian release: {recorded.value if recorded is not None else 'not recorded'}")
    output.info(f"Live Debian release: {observed.value}")

    changed = recorded is not observed
    if (
        changed
        and not yes
        and not output.confirm(
            f"Record Debian {observed.value} for VM '{name}' and require reinitialization?",
            default=False,
        )
    ):
        raise UserAbort("Debian release confirmation cancelled")

    with db.transaction():
        db.update_vm_debian_release(name, observed)
        if changed:
            db.update_vm_init_status(name, InitStatus.PENDING)

    if changed:
        output.result(
            f"VM '{name}' now records Debian {observed.value}. "
            f"Run 'agw vm reinit {name}' before relying on release-aware initialization."
        )
    else:
        output.result(f"VM '{name}' Debian {observed.value} observation refreshed.")
    return observed
