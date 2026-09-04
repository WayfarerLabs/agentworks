"""Read-only multi-VM status observation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import SYSTEM_SLUG_KEY, VMStatus
from agentworks.errors import AgentworksError, UserAbort

from ._helpers import _vm_scope
from .boundary import _platform_ops_ctx

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.vms.nodes import LiveVMNode


def project_vm_status(status: VMStatus, *, operator_stopped: bool) -> tuple[str, str | None]:
    """Project provider power state and stored operator intent for display."""
    disposition = None
    if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        disposition = "manual" if operator_stopped else "idle"
    return status.value, disposition


def observe_vm_statuses(
    db: Database,
    config: Config,
    vms: Sequence[VMRow],
    *,
    interaction: TtyInteractionPolicy,
) -> dict[str, VMStatus]:
    """Observe selected VMs without activation, repair, or persistence."""
    from concurrent.futures import as_completed
    from functools import partial

    from agentworks.bootstrap import load_request_registry
    from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.status_observation import cancelling_futures
    from agentworks.vms.nodes import VMSiteNode, live_vm_node

    statuses = {vm.name: VMStatus.UNKNOWN for vm in vms}
    if not vms:
        return statuses

    try:
        registry = load_request_registry(config, live_database=db)
    except UserAbort:
        raise
    except AgentworksError:
        return statuses

    site_nodes: dict[str, VMSiteNode] = {}
    vm_nodes: list[LiveVMNode] = []
    for vm in vms:
        try:
            vm_nodes.append(live_vm_node(db, config, registry, vm, site_nodes=site_nodes))
        except UserAbort:
            raise
        except AgentworksError:
            continue

    if not vm_nodes:
        return statuses

    try:
        nodes = walk(*vm_nodes)
        resolver = Resolver(config, registry, interaction=interaction)
        for secret_name in secret_union(nodes):
            resolver.register_name(secret_name)
        system_scope = OperationScope(
            level=ScopeLevel.SYSTEM,
            system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        )
        preflight_all(
            nodes,
            RunContext(config=config, operation_scope=system_scope),
            registry=registry,
            interaction=interaction,
        )
        resolver.resolve()
    except UserAbort:
        raise
    except AgentworksError:
        return statuses

    by_site: dict[str, list[tuple[LiveVMNode, RunContext]]] = {}
    for node in vm_nodes:
        try:
            context = _platform_ops_ctx(config, _vm_scope(db, node.row.name), node, resolver)
        except UserAbort:
            raise
        except AgentworksError:
            continue
        by_site.setdefault(node.row.site, []).append((node, context))

    def observe_site(site_name: str) -> dict[str, VMStatus]:
        observed: dict[str, VMStatus] = {}
        for node, context in by_site[site_name]:
            row = node.row
            try:
                status = node.site.platform.status(row, context)
            except UserAbort:
                raise
            except AgentworksError:
                continue
            if not isinstance(status, VMStatus):
                raise TypeError(f"VM platform returned non-VMStatus for '{row.name}'")
            observed[row.name] = status
        return observed

    tasks = {site_name: partial(observe_site, site_name) for site_name in by_site}
    with cancelling_futures(tasks) as futures:
        for future in as_completed(futures):
            statuses.update(future.result())
    return statuses
