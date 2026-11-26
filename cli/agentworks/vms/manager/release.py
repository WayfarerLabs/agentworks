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
from agentworks.recheck import require_unchanged, vm_release_fields

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
    """Observe and explicitly adopt a recognized live Debian release.

    The confirmation is asked with no native mutation guard held. Under the
    guard, the VM row and the live release are read again, and a change in
    either refuses before anything is written.
    """

    import agentworks.execution as execution
    from agentworks.bootstrap import load_request_registry
    from agentworks.execution.access import Admin
    from agentworks.vms.contract import observe_release

    from ._helpers import _guard_incomplete_vm, _require_vm
    from .boundary import gated_vm_boundary

    # Before any database read or gate: an unsupported client refuses first.
    execution.resolve_ssh()
    vm = _require_vm(db, name)
    # A failed initialization only warns: reinit refuses release drift and names
    # this command, so refusing here would leave that VM with no way forward.
    _guard_incomplete_vm(vm, allow_failed_init=True)
    registry = load_request_registry(config, live_database=db)
    with gated_vm_boundary(db, config, registry, vm, interaction=interaction) as (vm_node, _resolver, _ctx):
        admitted = vm_node.row
        # Only the release request: adoption does not run the rest of the contract.
        observed = observe_release(execution.vm_access(admitted, config, Admin()), expected=None)

        recorded = admitted.debian_release
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

        # The answer is given; what it was about is read and observed again
        # under the guard before anything is written.
        with vm_node.mutation_guard():
            require_unchanged("vm", name, vm_release_fields(admitted), vm_release_fields(db.get_vm(name)))
            require_unchanged(
                "vm", name, observed, observe_release(execution.vm_access(admitted, config, Admin()), expected=None)
            )
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
