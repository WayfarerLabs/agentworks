"""Prepared provider operations for existing VMs, independent of intent acceptance."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.db import DesiredRuntimeState, VMStatus
from agentworks.errors import StateError
from agentworks.execution import trust
from agentworks.orchestration.secrets import ScopedSecrets

from ._helpers import _guard_incomplete_vm, _vm_scope

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.resources import Registry
    from agentworks.vms.nodes import LiveVMNode, VMSiteNode


@dataclass
class VMRuntimeContext:
    """Invocation-owned provider context and completed infrastructure effects."""

    db: Database
    config: Config
    vm: VMRow
    node: LiveVMNode
    ops_ctx: RunContext
    observed_status: VMStatus
    started: bool = False
    _provider_values: dict[str, str] = field(default_factory=dict, repr=False)

    def clear_values(self) -> None:
        """Dispose this invocation's retained provider values."""
        self._provider_values.clear()


def prepare_vm_runtime(
    db: Database,
    config: Config,
    registry: Registry,
    vm: VMRow,
    *,
    resolve_secret: Callable[[str], str],
    held_native_guard: NativeMutationGuard | None = None,
    site_nodes: dict[str, VMSiteNode] | None = None,
    vm_node: LiveVMNode | None = None,
) -> VMRuntimeContext:
    """Preflight and observe without activation, using the operation's gate callback."""
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import live_vm_node

    _guard_incomplete_vm(vm)
    if vm_node is None:
        vm_node = live_vm_node(db, config, registry, vm, held_native_guard=held_native_guard, site_nodes=site_nodes)
    if vm_node.row.vm_uuid != vm.vm_uuid or vm_node.row.name != vm.name:
        raise StateError("prepared VM node does not match selection", entity_kind="vm", entity_name=vm.name)
    vm_node.reload_row()
    scope = _vm_scope(db, vm.name)
    readiness_ctx = RunContext(config=config, operation_scope=scope)
    # Prepared operations share one Resolver. Source prediction here would
    # bypass its invocation failure memo and retry a shared failed credential.
    for node in walk(vm_node):
        node.preflight(readiness_ctx)
    names = vm_node.gate_secret_refs()
    values = {name: resolve_secret(name) for name in names}
    ops_ctx = RunContext(config=config, operation_scope=scope, secrets=ScopedSecrets(values, names))
    context = VMRuntimeContext(db, config, vm, vm_node, ops_ctx, VMStatus.UNKNOWN, _provider_values=values)
    try:
        context.observed_status = observe_vm_runtime(context)
    except BaseException:
        context.clear_values()
        raise
    return context


def _current_vm(context: VMRuntimeContext) -> VMRow:
    row = context.node.reload_row()
    if row.desired_runtime_state != context.vm.desired_runtime_state:
        raise StateError("selected VM target changed during this operation", entity_kind="vm", entity_name=row.name)
    return row


def observe_vm_runtime(context: VMRuntimeContext) -> VMStatus:
    """Observe only through the prepared provider context; never resolve or activate."""
    row = _current_vm(context)
    status = context.node.site.platform.status(row, context.ops_ctx)
    if not isinstance(status, VMStatus):
        raise StateError("VM provider returned an invalid power observation", entity_kind="vm", entity_name=row.name)
    return status


def activate_vm_runtime(context: VMRuntimeContext) -> bool:
    """Prepare guest access: start a provider-stopped VM, then wait for it to answer."""
    return _start_vm_runtime(context, explicit=False)


def start_vm_runtime(context: VMRuntimeContext) -> bool:
    """Apply an accepted explicit start."""
    return _start_vm_runtime(context, explicit=True)


def _start_vm_runtime(context: VMRuntimeContext, *, explicit: bool) -> bool:
    """Start the VM unless it is running, then wait inside a hold for it to answer over SSH.

    A VM with no host-key record is refused before its power is read, since
    the wait could only fail, and a VM that does not answer fails naming
    ``agw vm recover``, which repairs what a start cannot. An explicit start
    differs from an activation two ways. It proceeds on an unknown power
    observation. And it says what it did before the wait, so a failed wait
    still reads as one on a running VM.
    """
    import agentworks.vms.manager as manager

    row = _current_vm(context)
    if row.desired_runtime_state is DesiredRuntimeState.STOPPED:
        raise StateError("VM has stopped intent", entity_kind="vm", entity_name=row.name)
    trust.require_record(row.name)
    platform = context.node.site.platform
    status = observe_vm_runtime(context)
    if status is VMStatus.UNKNOWN and not explicit:
        raise StateError("VM power state is unknown", entity_kind="vm", entity_name=row.name)
    # Started before the hold: WSL2's keepalive boots a stopped distro as a
    # side effect of holding it, which would make the VM read as running.
    if status is not VMStatus.RUNNING:
        platform.start(row, context.ops_ctx)
        context.started = True
        if explicit:
            output.info(f"VM '{row.name}' started")
        if status is VMStatus.UNKNOWN:
            context.db.clear_vm_start_observation(row.name)
        else:
            context.db.record_vm_started(row.name)
    elif explicit:
        output.info(f"VM '{row.name}' is already running")
    with context.node.hold_active():
        manager._await_tailnet(context.db, context.config, row, already_running=status is VMStatus.RUNNING)
    return context.started


def execute_vm_stop(context: VMRuntimeContext) -> bool:
    """Stop through supplied provider values without accepting or rewriting intent."""
    return _stop_vm_runtime(context, allow_unknown=False)


def stop_vm_runtime(context: VMRuntimeContext) -> bool:
    """Apply an accepted explicit stop with the ordinary unknown-power policy."""
    return _stop_vm_runtime(context, allow_unknown=True)


def _stop_vm_runtime(context: VMRuntimeContext, *, allow_unknown: bool) -> bool:
    row = _current_vm(context)
    status = observe_vm_runtime(context)
    if status in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
        return False
    if status is VMStatus.UNKNOWN and not allow_unknown:
        raise StateError("VM power state is unknown", entity_kind="vm", entity_name=row.name)
    context.node.site.platform.stop(row, context.ops_ctx)
    return True
