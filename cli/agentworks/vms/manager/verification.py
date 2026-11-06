"""Non-activating VM connection verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import NotFoundError, StateError

from .boundary import _warn_legacy_release

_VERIFY_CONNECTION_TIMEOUT_SECONDS = 10

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.resources.registry import Registry


@dataclass(frozen=True, slots=True)
class VMConnectionVerification:
    """Result of one canonical transport no-op."""

    name: str
    transport: str


def verify_vm_connection(
    db: Database,
    config: Config,
    registry: Registry,
    name: str,
) -> VMConnectionVerification:
    """Prove the stored VM accepts one bounded, canonical, non-mutating command."""
    import agentworks.execution as execution
    from agentworks.execution.access import Admin
    from agentworks.execution.requests import Command

    # Before any database read: an unsupported client refuses first.
    execution.resolve_ssh()
    vm = db.get_vm(name)
    if vm is None:
        raise NotFoundError(
            f"VM '{name}' not found",
            entity_kind="vm",
            entity_name=name,
        )
    _warn_legacy_release(vm)

    # Resolve the VM's declared site through the usual readiness boundary, but
    # do not call any platform operation or activation machinery.
    from agentworks.vms.sites import resolve_site

    resolve_site(vm.site, registry)

    from agentworks.db import ProvisioningStatus
    from agentworks.execution.errors import TrustMissing
    from agentworks.vms.applied_state import VMSSHIdentityPolicyError
    from agentworks.vms.manager.boundary import require_vm_ssh_boundary

    try:
        require_vm_ssh_boundary(db, config, vm)
    except (TrustMissing, VMSSHIdentityPolicyError) as error:
        if vm.provisioning_status == ProvisioningStatus.COMPLETE.value:
            raise
        # Those hints name reinit, recover or host-key import, which all refuse such a VM.
        raise StateError(
            f"VM '{name}' has not completed provisioning, so its connection cannot be verified",
            entity_kind="vm",
            entity_name=name,
            hint=f"Only 'agw vm delete {name}' is supported on a VM whose provisioning did not complete.",
        ) from error

    access = execution.vm_access(vm, config, Admin())
    access.run(Command(("true",), deadline=_VERIFY_CONNECTION_TIMEOUT_SECONDS), check=True)
    # Every connection is SSH to the tailnet address; the kind is not a variable.
    return VMConnectionVerification(name=name, transport="ssh")
