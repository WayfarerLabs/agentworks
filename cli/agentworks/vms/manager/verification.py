"""Non-activating VM connection verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import NotFoundError

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
    vm = db.get_vm(name)
    if vm is None:
        raise NotFoundError(
            f"VM '{name}' not found",
            entity_kind="vm",
            entity_name=name,
        )

    # Resolve the VM's declared site through the usual readiness boundary, but
    # do not call any platform operation or activation machinery.
    from agentworks.vms.sites import resolve_site

    resolve_site(vm.site, registry)

    import agentworks.transports as transports

    target = transports.transport(vm, config)
    target.run("true", sudo=False, tty=False, env=None, timeout=_VERIFY_CONNECTION_TIMEOUT_SECONDS)
    description = target.describe()
    kind, separator, _endpoint = description.partition(":")
    return VMConnectionVerification(
        name=name,
        transport=kind if separator else description,
    )
