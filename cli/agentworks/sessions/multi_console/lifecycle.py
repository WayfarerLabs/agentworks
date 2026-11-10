"""Explicit console intent acceptance and invocation-owned runtime composition."""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.multi_console as _mc
from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.db import DesiredRuntimeState, VMStatus
from agentworks.errors import AgentworksError, NotFoundError, StateError, UserAbort
from agentworks.secrets.policy import require_exact_tty_interaction_policy

from ._helpers import _require_console, guarded_console_selection
from .runtime import validate_console_definition

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.resources.registry import Registry
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.vms.nodes import LiveVMNode


def preflight_console_vm(
    db: Database,
    config: Config,
    registry: Registry,
    vm: VMRow,
    *,
    held_native_guard: NativeMutationGuard,
    interaction: TtyInteractionPolicy,
) -> LiveVMNode:
    """Check known local VM and provider policies before accepting intent."""
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.walk import walk
    from agentworks.vms.manager import require_vm_ssh_boundary
    from agentworks.vms.manager._helpers import _guard_failed_vm, _vm_scope
    from agentworks.vms.nodes import live_vm_node

    _guard_failed_vm(vm)
    require_vm_ssh_boundary(db, config, vm)
    node = live_vm_node(db, config, registry, vm, held_native_guard=held_native_guard)
    preflight_all(
        walk(node),
        RunContext(config=config, operation_scope=_vm_scope(db, vm.name)),
        registry=registry,
        interaction=interaction,
    )
    return node


def realize_console(
    db: Database,
    config: Config,
    *,
    name: str,
    replace_running: bool,
    interaction: TtyInteractionPolicy,
) -> None:
    """Accept running, prepare infrastructure, then resolve and apply one workload."""
    from agentworks.bootstrap import load_request_registry
    from agentworks.secrets.resolver import Resolver
    from agentworks.transports import transport
    from agentworks.vms.manager.runtime import activate_vm_runtime, prepare_vm_runtime

    require_exact_tty_interaction_policy(interaction)
    with guarded_console_selection(db, name) as (console, guard):
        validate_console_definition(db, console)
        vm = db.get_vm(console.vm_name)
        if vm is None:
            raise NotFoundError(f"VM '{console.vm_name}' not found", entity_kind="vm", entity_name=console.vm_name)
        if vm.tailscale_host is None:
            raise StateError(f"VM '{vm.name}' has no Tailscale address", entity_kind="vm", entity_name=vm.name)
        registry = load_request_registry(config, live_database=db)
        node = preflight_console_vm(
            db,
            config,
            registry,
            vm,
            held_native_guard=guard,
            interaction=interaction,
        )
        _mc.accept_console_running(db, console=console, held_native_guard=guard)
        console = _require_console(db, name)
        if vm.desired_runtime_state is DesiredRuntimeState.STOPPED:
            raise StateError(
                "console start is blocked by its stopped parent VM", entity_kind="console", entity_name=name
            )
        resolver = Resolver(config, registry, interaction=interaction)
        context = None
        values: dict[str, str] = {}
        try:
            context = prepare_vm_runtime(
                db,
                config,
                registry,
                vm,
                resolve_secret=resolver.resolve_gate,
                interaction=interaction,
                held_native_guard=guard,
                vm_node=node,
            )
            activate_vm_runtime(context, resolve_secret=resolver.resolve_gate)
            with context.node.hold_active():
                prepared = _mc.prepare_console_reconcile(
                    db,
                    registry,
                    console=console,
                    vm=vm,
                    target=transport(vm, config),
                    held_native_guard=guard,
                    replace_running=replace_running,
                )
                resolver.register_targets(prepared.pane_targets, allow_transient_auto_declare=True)
                resolver.resolve()
                values = resolver.values
                changed = _mc.execute_console_reconcile(prepared, values=values)
        finally:
            values.clear()
            if context is not None:
                context.clear_values()
            resolver.close()
        if changed:
            output.result(f"Console '{name}' {'restarted' if replace_running else 'started'}")
        else:
            output.result(f"Console '{name}' is already running")


def stop_console_runtime(
    db: Database,
    config: Config,
    *,
    name: str,
    interaction: TtyInteractionPolicy,
) -> None:
    """Accept stopped and tear down using provider observation without activation."""
    from agentworks.bootstrap import load_request_registry
    from agentworks.secrets.resolver import Resolver
    from agentworks.transports import transport
    from agentworks.vms.manager.runtime import observe_vm_runtime, prepare_vm_runtime

    require_exact_tty_interaction_policy(interaction)
    with guarded_console_selection(db, name) as (console, guard):
        vm = db.get_vm(console.vm_name)
        if vm is None:
            raise NotFoundError(f"VM '{console.vm_name}' not found", entity_kind="vm", entity_name=console.vm_name)
        registry = load_request_registry(config, live_database=db)
        node = preflight_console_vm(
            db,
            config,
            registry,
            vm,
            held_native_guard=guard,
            interaction=interaction,
        )
        db.set_console_desired_runtime_state(name, DesiredRuntimeState.STOPPED)
        console = _require_console(db, name)
        resolver = Resolver(config, registry, interaction=interaction)
        context = None
        try:
            context = prepare_vm_runtime(
                db,
                config,
                registry,
                vm,
                resolve_secret=resolver.resolve_gate,
                interaction=interaction,
                held_native_guard=guard,
                vm_node=node,
            )
            status = context.observed_status
            if status is VMStatus.UNKNOWN:
                raise StateError("parent VM power state is unknown", entity_kind="console", entity_name=name)
            target = None if status in {VMStatus.STOPPED, VMStatus.DEALLOCATED} else transport(vm, config)
            try:
                prepared = _mc.prepare_console_stop(
                    db,
                    console=console,
                    vm=vm,
                    target=target,
                    held_native_guard=guard,
                )
                resolver.resolve()
                if target is None:
                    current_status = observe_vm_runtime(context)
                    if current_status is VMStatus.UNKNOWN:
                        raise StateError("parent VM power state is unknown", entity_kind="console", entity_name=name)
                    if current_status is VMStatus.RUNNING:
                        prepared = _mc.prepare_console_stop(
                            db,
                            console=console,
                            vm=vm,
                            target=transport(vm, config),
                            held_native_guard=guard,
                        )
                _mc.execute_console_stop(prepared)
            except UserAbort:
                raise
            except AgentworksError:
                if observe_vm_runtime(context) not in {VMStatus.STOPPED, VMStatus.DEALLOCATED}:
                    raise
        finally:
            if context is not None:
                context.clear_values()
            resolver.close()
        output.result(f"Console '{name}' stopped")


def attach_running_console(
    db: Database,
    config: Config,
    *,
    name: str,
    interaction: TtyInteractionPolicy,
) -> int:
    """Guard runtime preparation, then release the family during interaction."""
    from contextlib import ExitStack

    from agentworks.bootstrap import load_request_registry
    from agentworks.secrets.resolver import Resolver
    from agentworks.sessions.tmux import ProbeStatus, exact_tmux_target
    from agentworks.terminal import clear_screen_on_detach
    from agentworks.transports import transport
    from agentworks.vms.manager.runtime import activate_vm_runtime, prepare_vm_runtime

    from ._helpers import tmux_session_name

    require_exact_tty_interaction_policy(interaction)
    context = None
    resolver = None
    try:
        with ExitStack() as holds:
            with guarded_console_selection(db, name) as (console, guard):
                vm = db.get_vm(console.vm_name)
                if vm is None:
                    raise NotFoundError(
                        f"VM '{console.vm_name}' not found", entity_kind="vm", entity_name=console.vm_name
                    )
                if vm.tailscale_host is None:
                    raise StateError(f"VM '{vm.name}' has no Tailscale address", entity_kind="vm", entity_name=vm.name)
                registry = load_request_registry(config, live_database=db)
                node = preflight_console_vm(
                    db,
                    config,
                    registry,
                    vm,
                    held_native_guard=guard,
                    interaction=interaction,
                )
                resolver = Resolver(config, registry, interaction=interaction)
                context = prepare_vm_runtime(
                    db,
                    config,
                    registry,
                    vm,
                    resolve_secret=resolver.resolve_gate,
                    interaction=interaction,
                    held_native_guard=guard,
                    vm_node=node,
                )
                activate_vm_runtime(context, resolve_secret=resolver.resolve_gate)
                holds.enter_context(context.node.hold_active())
                target = transport(vm, config)
                canonical, staging = _mc._console_runtime_presence(target, name)
                if canonical is not ProbeStatus.PRESENT or staging is not ProbeStatus.ABSENT:
                    raise StateError(
                        f"console '{name}' is not ready to attach",
                        entity_kind="console",
                        entity_name=name,
                        hint=f"Run `agw console start {name}`.",
                    )
            return target.interactive(
                f"tmux attach -t {exact_tmux_target(tmux_session_name(name))}",
                clear_screen_on_exit=clear_screen_on_detach(config.terminal.clear_on_detach),
            )
    finally:
        if context is not None:
            context.clear_values()
        if resolver is not None:
            resolver.close()
