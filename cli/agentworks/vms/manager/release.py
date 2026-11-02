"""Reconciling an observed Debian release with the recorded one, and explicit adoption.

The observation itself is the VM contract's (``contract.observe_release``),
over ``agentworks.execution``, so a VM without python3 cannot be observed and
raises ``VMNotReady``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import InitStatus
from agentworks.errors import StateError, UserAbort

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.debian import DebianRelease
    from agentworks.secrets.policy import TtyInteractionPolicy


def reconcile_release(db: Database, vm: VMRow, observed: DebianRelease) -> DebianRelease:
    """Persist ``observed`` as ``vm``'s release and return it, refusing drift.

    This ordinary service may fill an unknown row or refresh a matching
    observation. It refuses a live release that differs from the recorded
    one: ``vm confirm-release`` owns explicit adoption when an operator
    changed the guest outside Agentworks.
    """
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

    import agentworks.execution as execution
    from agentworks.bootstrap import load_request_registry
    from agentworks.execution.access import Admin
    from agentworks.vms.contract import observe_release

    from ._helpers import _require_vm
    from .boundary import gated_vm_boundary

    vm = _require_vm(db, name)
    registry = load_request_registry(config, live_database=db)
    with gated_vm_boundary(db, config, registry, vm, interaction=interaction):
        # Only the release request: adoption does not run the rest of the contract.
        observed = observe_release(execution.vm_access(vm, config, Admin()), expected=None)

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
