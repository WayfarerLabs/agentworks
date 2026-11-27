"""Each VM family's lock set: probed at (b), briefly held to record intent at (d), and held from (f) to the end.

A family's set holds its live targets (sessions and consoles) exclusive and
the vm shared, or the vm exclusive when the VM's own target is stopped,
since the family then stops it. One rule covers every acquisition: a
refusal at an entity other than the vm drops every target that needs that
entity in the refused mode, and the family retries with the rest, so each
attempt is all or nothing and only ever smaller. A refused vm blocks the
whole family.
"""

from __future__ import annotations

import contextlib
from functools import partial
from typing import TYPE_CHECKING

from agentworks.db import DesiredRuntimeState
from agentworks.errors import StateConflictError, StateError
from agentworks.native_locks import LockRequest, NativeLockBusyError, native_locks, probe_locks, vm_entity
from agentworks.native_locks.rows import console_entity_for, session_entity_for
from agentworks.recheck import require_request_unchanged, session_fields, vm_fields

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from agentworks.native_locks import Entity, NativeLockSet
    from agentworks.vms.nodes import LiveVMNode

    from ._models import Kind, ResourceKey, RuntimeInvocation


def _targets(invocation: RuntimeInvocation, vm_name: str) -> dict[ResourceKey, Entity]:
    """The family's live targets and their entities; a target whose rows have gone fails alone."""
    routes = dict(invocation.selection.session_routes)
    built: list[tuple[ResourceKey, Callable[[], Entity]]] = []
    for name, session in invocation.sessions.items():
        if routes.get(name) == vm_name and not invocation.unavailable("session", name):
            built.append((("session", name), partial(session_entity_for, invocation.db, session)))
    for name, console in invocation.consoles.items():
        if console.vm_name == vm_name and not invocation.unavailable("console", name):
            built.append((("console", name), partial(console_entity_for, invocation.db, console)))
    targets: dict[ResourceKey, Entity] = {}
    for key, entity in built:
        try:
            targets[key] = entity()
        except StateConflictError as vanished:
            # A row the target's entity is built from went away: that target fails, the family goes on.
            invocation.fail(*key, vanished)
    return targets


def _stops_vm(invocation: RuntimeInvocation, vm_name: str) -> bool:
    return (
        vm_name in invocation.selection.explicit_vms
        and invocation.vms[vm_name].desired_runtime_state is DesiredRuntimeState.STOPPED
    )


def _request(invocation: RuntimeInvocation, vm_name: str, targets: dict[ResourceKey, Entity]) -> LockRequest:
    vm = vm_entity(vm_name)
    if _stops_vm(invocation, vm_name):
        return LockRequest(exclusive=(vm, *targets.values()))
    return LockRequest(exclusive=tuple(targets.values()), shared=(vm,))


def _needs(target: Entity, refused: NativeLockBusyError) -> bool:
    mode = LockRequest(exclusive=(target,)).modes().get(refused.entity)
    return mode is not None and mode >= refused.mode


def _lock[T](invocation: RuntimeInvocation, vm_name: str, acquire: Callable[[LockRequest], T]) -> T | None:
    """Acquire the family's set by the per-target rule; ``None`` when the whole family is refused."""
    while True:
        targets = _targets(invocation, vm_name)
        if not targets and vm_name not in invocation.selection.explicit_vms:
            return None
        try:
            return acquire(_request(invocation, vm_name, targets))
        except NativeLockBusyError as busy:
            dropped = [key for key, target in targets.items() if _needs(target, busy)]
            if busy.entity_kind == "vm" or not dropped:
                block_family(invocation, vm_name, busy)
                return None
            for kind, name in dropped:
                invocation.refuse(kind, name, busy)


def block_family(invocation: RuntimeInvocation, vm_name: str, error: Exception) -> None:
    """Refuse the whole family: the VM's result, if selected, and every live target."""
    invocation.failed_families.add(vm_name)
    if ("vm", vm_name) in invocation.results:
        invocation.fail("vm", vm_name, error, blocked=True)
    routes = dict(invocation.selection.session_routes)
    kinds: list[tuple[Kind, str]] = [("session", name) for name in invocation.sessions if routes.get(name) == vm_name]
    kinds.extend(("console", name) for name, console in invocation.consoles.items() if console.vm_name == vm_name)
    for kind, name in kinds:
        if not invocation.unavailable(kind, name):
            invocation.refuse(kind, name, error)


def _families(invocation: RuntimeInvocation) -> list[str]:
    return sorted(name for name in invocation.vms if name not in invocation.failed_families)


def probe_families(invocation: RuntimeInvocation) -> None:
    """(b): try each family's set without holding it, so a conflict refuses before any prompt or boot."""
    for vm_name in _families(invocation):
        _lock(invocation, vm_name, lambda request: probe_locks(invocation.db.path, request))


@contextlib.contextmanager
def intent_lock(invocation: RuntimeInvocation, vm_name: str) -> Iterator[NativeLockSet | None]:
    """(d): the family's set, held briefly while its intent is recorded; ``None`` when refused."""
    with contextlib.ExitStack() as brief:
        held = _lock(
            invocation, vm_name, lambda request: brief.enter_context(native_locks(invocation.db.path, request))
        )
        yield held


def acquire_final_sets(invocation: RuntimeInvocation) -> None:
    """(f) and (g): every surviving family's set in VM name order, held to the end and re-checked.

    Each set is acquired as one sanctioned to prompt, for the workload and
    pane secrets the boundary resolves under it.
    """
    for vm_name in _families(invocation):
        node = invocation.nodes[vm_name]

        def hold(request: LockRequest, node: LiveVMNode = node) -> NativeLockSet:
            return invocation.stack.enter_context(node.locked(request, prompts=True))

        held = _lock(invocation, vm_name, hold)
        if held is not None:
            _recheck(invocation, vm_name, held)


def _recheck(invocation: RuntimeInvocation, vm_name: str, held: NativeLockSet) -> None:
    """(g): the VM row; each target's identity, recorded intent, declaration and launch inputs; console members."""
    import agentworks.sessions.manager as sessions

    db = invocation.db
    routes = dict(invocation.selection.session_routes)
    if vm_fields(db.get_vm(vm_name)) != vm_fields(invocation.vms[vm_name]):
        block_family(invocation, vm_name, _changed("vm", vm_name))
        return
    for name, session in invocation.sessions.items():
        if invocation.unavailable("session", name) or routes.get(name) != vm_name:
            continue
        current = db.get_session(name)
        if (
            session_fields(current) != session_fields(session)
            or current is None
            or current.desired_runtime_state != session.desired_runtime_state
            or db.instance_state.get_desired_overlay("session", name) != invocation.accepted_overlays.get(name)
        ):
            invocation.refuse("session", name, _changed("session", name))
            continue
        definition = invocation.definitions.get(name)
        if definition is not None:
            # The launch composes from its definition's rows: a rehome, rebinding or new layer since refuses it.
            try:
                sessions.require_launch_inputs_unchanged(definition)
            except StateConflictError as error:
                invocation.refuse("session", name, error)
    for name, console in invocation.consoles.items():
        if console.vm_name != vm_name or invocation.unavailable("console", name):
            continue
        current_console = db.get_console(name)
        if current_console is None or (current_console.vm_name, current_console.desired_runtime_state) != (
            console.vm_name,
            console.desired_runtime_state,
        ):
            invocation.refuse("console", name, _changed("console", name))
            continue
        try:
            # The members as they are now, against the ones the console was locked with.
            members_now = console_entity_for(db, current_console)
        except StateConflictError as error:
            invocation.refuse("console", name, error)
            continue
        locked = next((entity for entity in held.modes if entity == members_now), None)
        if locked is None:
            # Every live console target is in the family's set, so this is a wiring bug.
            raise StateError(
                f"console '{name}' was re-checked without its lock", entity_kind="console", entity_name=name
            )
        try:
            require_request_unchanged(
                "console", name, LockRequest(exclusive=(locked,)), LockRequest(exclusive=(members_now,))
            )
        except StateConflictError as error:
            invocation.refuse("console", name, error)


def _changed(kind: str, name: str) -> StateConflictError:
    return StateConflictError(
        f"{kind} '{name}' changed since this command read it",
        entity_kind=kind,
        entity_name=name,
        hint="Run the command again.",
    )


def held_for(invocation: RuntimeInvocation, vm_name: str) -> NativeLockSet:
    """The final set a family holds, for the nested operations of (h)."""
    held = invocation.nodes[vm_name].held
    if held is None:
        raise StateError(f"VM '{vm_name}' family runs without its lock set", entity_kind="vm", entity_name=vm_name)
    return held
