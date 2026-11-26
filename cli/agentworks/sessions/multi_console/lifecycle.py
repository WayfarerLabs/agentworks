"""Explicit console intent acceptance and invocation-owned runtime composition."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING

import agentworks.sessions.multi_console as _mc
from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.db import DesiredRuntimeState, VMStatus
from agentworks.errors import AgentworksError, NotFoundError, StateConflictError, StateError, UserAbort
from agentworks.intent_restore import replaced_intent
from agentworks.secrets.policy import require_exact_tty_interaction_policy

from ._helpers import _require_console, console_request, locked_console, probe_console
from .runtime import validate_console_definition

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.config import Config
    from agentworks.db import ConsoleRow, Database, VMRow
    from agentworks.native_locks import NativeLockSet
    from agentworks.resources.registry import Registry
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.vms.nodes import LiveVMNode


def preflight_console_vm(
    db: Database,
    config: Config,
    registry: Registry,
    vm: VMRow,
    *,
    interaction: TtyInteractionPolicy,
) -> LiveVMNode:
    """Check known local VM and provider policies before accepting intent."""
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.walk import walk
    from agentworks.vms.manager._helpers import _guard_incomplete_vm, _vm_scope
    from agentworks.vms.nodes import live_vm_node

    _guard_incomplete_vm(vm)
    node = live_vm_node(db, config, registry, vm)
    preflight_all(
        walk(node),
        RunContext(config=config, operation_scope=_vm_scope(db, vm.name)),
        registry=registry,
        interaction=interaction,
    )
    return node


@contextmanager
def _final_set(
    node: LiveVMNode, db: Database, console: ConsoleRow, *, restore: tuple[str, ...] | None
) -> Iterator[tuple[NativeLockSet, ConsoleRow]]:
    """The console's final set, and the console as it reads under it.

    Under the set the VM row the gate admitted, the console's members and
    the intent this command recorded are read again, and a change refuses.
    A refusal, busy or changed, says how to restore that intent, if the
    command changed it.
    """
    from agentworks.intent_restore import refused_after_intent
    from agentworks.native_locks import NativeLockBusyError
    from agentworks.recheck import require_request_unchanged, require_vm_unchanged

    request = console_request(db, console)
    with ExitStack() as stack:
        try:
            held = stack.enter_context(node.locked(request, prompts=True))
            require_vm_unchanged(db, node.row)
            require_request_unchanged(
                "console", console.name, request, console_request(db, _require_console(db, console.name))
            )
            current = _require_intent_unchanged(db, console)
        except (NativeLockBusyError, StateConflictError) as refused:
            refused_after_intent(refused, restore)
            raise
        yield held, current


def _require_intent_unchanged(db: Database, accepted: ConsoleRow) -> ConsoleRow:
    """Under the final set: the console row and the intent it recorded still stand."""
    from agentworks.recheck import console_row_fields, require_unchanged

    current = _require_console(db, accepted.name)
    require_unchanged(
        "console",
        accepted.name,
        (console_row_fields(db, accepted.name), accepted.desired_runtime_state),
        (console_row_fields(db, current.name), current.desired_runtime_state),
    )
    return current


def realize_console(
    db: Database,
    config: Config,
    *,
    name: str,
    replace_running: bool,
    interaction: TtyInteractionPolicy,
) -> None:
    """Accept running, prepare infrastructure, then resolve and apply one workload.

    The known-policy preflight runs first; running intent is then recorded
    under a brief console exclusive, so it survives any later failure. The
    gate secrets resolve and the VM is observed and, if stopped, booted
    under a brief vm exclusive, with the console unlocked. The console's
    final set is held from the pane secrets to the end: it is one of the
    acquisitions sanctioned to prompt, since which secrets the panes need
    depends on what is observed under it.
    """
    import agentworks.execution as execution
    from agentworks.bootstrap import load_request_registry
    from agentworks.execution.access import Admin
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.manager import require_vm_ssh_boundary
    from agentworks.vms.manager.runtime import activate_vm_runtime, prepare_vm_runtime

    require_exact_tty_interaction_policy(interaction)
    console = _require_console(db, name)
    validate_console_definition(db, console)
    vm = db.get_vm(console.vm_name)
    if vm is None:
        raise NotFoundError(f"VM '{console.vm_name}' not found", entity_kind="vm", entity_name=console.vm_name)
    if vm.tailscale_host is None:
        raise StateError(f"VM '{vm.name}' has no Tailscale address", entity_kind="vm", entity_name=vm.name)
    probe_console(db, name)
    registry = load_request_registry(config, live_database=db)
    require_vm_ssh_boundary(db, config, vm)
    node = preflight_console_vm(db, config, registry, vm, interaction=interaction)
    with locked_console(db, name) as (selected, brief):
        validate_console_definition(db, selected)
        _mc.accept_console_running(db, console=selected, held=brief)
    restore = replaced_intent("console", name, selected.desired_runtime_state, DesiredRuntimeState.RUNNING)
    console = _require_console(db, name)
    if vm.desired_runtime_state is DesiredRuntimeState.STOPPED:
        raise StateError("console start is blocked by its stopped parent VM", entity_kind="console", entity_name=name)
    resolver = Resolver(config, registry, interaction=interaction)
    context = None
    values: dict[str, str] = {}
    try:
        context = prepare_vm_runtime(db, config, registry, vm, resolve_secret=resolver.resolve_gate, vm_node=node)
        activate_vm_runtime(context)
        with _final_set(node, db, console, restore=restore) as (held, console), context.node.hold_active():
            # The body runs on the row the hold admitted.
            vm = context.node.row
            prepared = _mc.prepare_console_reconcile(
                db,
                config,
                registry,
                console=console,
                vm=vm,
                admin=execution.vm_access(vm, config, Admin()),
                held=held,
                replace_running=replace_running,
            )
            resolver.register_targets(prepared.pane_targets, allow_transient_auto_declare=True)
            resolver.resolve()
            values = resolver.values
            _mc.validate_console_values(prepared, values)
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
    """Accept stopped and tear down using provider observation without activation.

    Stopped intent is recorded under a brief console exclusive; the gate
    secrets resolve and the VM is observed unlocked; the teardown runs
    under the console's final set.
    """
    import agentworks.execution as execution
    from agentworks.bootstrap import load_request_registry
    from agentworks.execution.access import Admin
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.manager import require_vm_ssh_boundary
    from agentworks.vms.manager.runtime import observe_vm_runtime, prepare_vm_runtime

    require_exact_tty_interaction_policy(interaction)
    selected = _require_console(db, name)
    vm = db.get_vm(selected.vm_name)
    if vm is None:
        raise NotFoundError(f"VM '{selected.vm_name}' not found", entity_kind="vm", entity_name=selected.vm_name)
    probe_console(db, name)
    registry = load_request_registry(config, live_database=db)
    node = preflight_console_vm(db, config, registry, vm, interaction=interaction)
    with locked_console(db, name):
        previous = _require_console(db, name).desired_runtime_state
        db.set_console_desired_runtime_state(name, DesiredRuntimeState.STOPPED)
    restore = replaced_intent("console", name, previous, DesiredRuntimeState.STOPPED)
    console = _require_console(db, name)
    resolver = Resolver(config, registry, interaction=interaction)
    context = None
    try:
        context = prepare_vm_runtime(db, config, registry, vm, resolve_secret=resolver.resolve_gate, vm_node=node)
        with _final_set(node, db, console, restore=restore) as (held, console):
            # The body runs on the row the gate admitted, the one the final set compared.
            vm = context.node.row
            status = context.observed_status
            if status is VMStatus.UNKNOWN:
                raise StateError("parent VM power state is unknown", entity_kind="console", entity_name=name)
            admin = None
            if status not in {VMStatus.STOPPED, VMStatus.DEALLOCATED}:
                require_vm_ssh_boundary(db, config, vm)
                admin = execution.vm_access(vm, config, Admin())
            try:
                prepared = _mc.prepare_console_stop(db, console=console, vm=vm, admin=admin, held=held)
                resolver.resolve()
                if admin is None:
                    current_status = observe_vm_runtime(context)
                    if current_status is VMStatus.UNKNOWN:
                        raise StateError("parent VM power state is unknown", entity_kind="console", entity_name=name)
                    if current_status is VMStatus.RUNNING:
                        require_vm_ssh_boundary(db, config, vm)
                        prepared = _mc.prepare_console_stop(
                            db,
                            console=console,
                            vm=vm,
                            admin=execution.vm_access(vm, config, Admin()),
                            held=held,
                        )
                _mc.execute_console_stop(prepared)
                observation_from_parent = prepared.admin is None
            except UserAbort:
                raise
            except AgentworksError:
                if observe_vm_runtime(context) not in {VMStatus.STOPPED, VMStatus.DEALLOCATED}:
                    raise
                observation_from_parent = True
    finally:
        if context is not None:
            context.clear_values()
        resolver.close()
    _report_console_stop(name, observation_from_parent=observation_from_parent)


def _report_console_stop(name: str, *, observation_from_parent: bool) -> None:
    qualification = (
        "; parent VM inactive, guest uninspected for this observation; "
        "inactivity alone does not clear recorded runtime evidence"
        if observation_from_parent
        else ""
    )
    output.result(f"Console '{name}' stopped{qualification}")


def attach_running_console(
    db: Database,
    config: Config,
    *,
    name: str,
    interaction: TtyInteractionPolicy,
) -> int:
    """Resolve the gate secrets and observe the VM unlocked, admit the hold, then attach.

    Console attach is an access command: its whole lock is the admission's
    brief vm shared (and a brief vm exclusive for a boot), so only a
    VM-wide operation refuses it.
    """
    from contextlib import ExitStack

    import agentworks.execution as execution
    from agentworks.bootstrap import load_request_registry
    from agentworks.execution.access import Admin
    from agentworks.execution.requests import TerminalCommand
    from agentworks.native_locks import probe_locks
    from agentworks.native_locks.rows import admission
    from agentworks.recheck import console_fields, require_unchanged
    from agentworks.secrets.resolver import Resolver
    from agentworks.sessions.tmux import ProbeStatus
    from agentworks.terminal import clear_screen_on_detach
    from agentworks.vms.manager import require_vm_ssh_boundary
    from agentworks.vms.manager.runtime import activate_vm_runtime, prepare_vm_runtime

    from ._helpers import tmux_session_name

    require_exact_tty_interaction_policy(interaction)
    context = None
    resolver = None
    try:
        with ExitStack() as holds:
            console = _require_console(db, name)
            read = console_fields(db, name)
            vm = db.get_vm(console.vm_name)
            if vm is None:
                raise NotFoundError(f"VM '{console.vm_name}' not found", entity_kind="vm", entity_name=console.vm_name)
            if vm.tailscale_host is None:
                raise StateError(f"VM '{vm.name}' has no Tailscale address", entity_kind="vm", entity_name=vm.name)
            registry = load_request_registry(config, live_database=db)
            require_vm_ssh_boundary(db, config, vm)
            node = preflight_console_vm(db, config, registry, vm, interaction=interaction)
            probe_locks(db.path, admission(vm.name))
            # The gate secrets resolve, and the VM is observed, with nothing locked.
            resolver = Resolver(config, registry, interaction=interaction)
            context = prepare_vm_runtime(db, config, registry, vm, resolve_secret=resolver.resolve_gate, vm_node=node)
            # An access command: the VM boots, if it needs to, under a brief vm
            # exclusive, the hold is admitted under a brief vm shared, and the
            # readiness probe and the attach run unlocked.
            require_unchanged("console", name, read, console_fields(db, name))
            # The VM's intent and address as they are now, not as read before the secrets.
            vm = context.node.reload_row()
            if vm.desired_runtime_state is DesiredRuntimeState.STOPPED:
                if context.observed_status is not VMStatus.RUNNING:
                    raise StateError(
                        "console attach requires an already-running parent VM with stopped intent",
                        entity_kind="console",
                        entity_name=name,
                    )
            else:
                activate_vm_runtime(context)
            holds.enter_context(context.node.hold_active())
            # The row the hold admitted.
            admin = execution.vm_access(context.node.row, config, Admin())
            canonical, staging = _mc._console_runtime_presence(admin, name)
            if canonical is not ProbeStatus.PRESENT or staging is not ProbeStatus.ABSENT:
                raise StateError(
                    f"console '{name}' is not ready to attach",
                    entity_kind="console",
                    entity_name=name,
                    hint=f"Run `agw console start {name}`.",
                )
            return admin.attach(
                TerminalCommand(("tmux", "attach", "-t", "=" + tmux_session_name(name))),
                clear_screen_on_exit=clear_screen_on_detach(config.terminal.clear_on_detach),
            )
    finally:
        if context is not None:
            context.clear_values()
        if resolver is not None:
            resolver.close()
