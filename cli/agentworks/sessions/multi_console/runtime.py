"""Guarded named-console preparation and value-only runtime execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING

import agentworks.sessions.multi_console as _mc
from agentworks.db import DesiredRuntimeState
from agentworks.errors import NotFoundError, StateError, ValidationError
from agentworks.harness_setup.locking import native_mutation_guard
from agentworks.resources.access import named_console_template
from agentworks.sessions.tmux import ProbeStatus

from ._helpers import _require_console, _verify_session_on_vm, guarded_console_selection
from ._status import ConsoleStatus, classify_console_status

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.db import ConsoleRow, ConsoleSessionRow, Database, VMRow
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.resources.registry import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.transports import Transport


class ConsoleAction(Enum):
    UNCHANGED = "unchanged"
    CLEANUP_STAGING = "cleanup-staging"
    BUILD = "build"
    REPLACE = "replace"
    STOP = "stop"


@dataclass(frozen=True)
class PreparedConsoleReconcile:
    """A definition and exact runtime observation retained under a family guard."""

    db: Database
    registry: Registry
    console: ConsoleRow
    vm: VMRow
    target: Transport | None
    held_native_guard: NativeMutationGuard
    members: tuple[ConsoleSessionRow, ...]
    layout: str
    canonical: ProbeStatus
    staging: ProbeStatus
    action: ConsoleAction
    pane_targets: tuple[SecretTarget, ...]


@dataclass(frozen=True)
class PreparedConsoleStop:
    db: Database
    console: ConsoleRow
    vm: VMRow
    target: Transport | None
    held_native_guard: NativeMutationGuard
    members: tuple[ConsoleSessionRow, ...]
    canonical: ProbeStatus
    staging: ProbeStatus
    action: ConsoleAction


def validate_console_definition(db: Database, console: ConsoleRow) -> tuple[ConsoleSessionRow, ...]:
    """Validate stored relationships before accepting an explicit start."""
    members = tuple(db.list_console_sessions(console.name))
    if not members and not console.admin_shell:
        raise StateError(
            f"console '{console.name}' has no configured windows",
            entity_kind="console",
            entity_name=console.name,
        )
    for member in members:
        _verify_session_on_vm(db, member.session_name, console.vm_name)
        session = db.get_session(member.session_name)
        assert session is not None
        vm = db.get_vm(console.vm_name)
        if vm is None:
            raise NotFoundError(f"VM '{console.vm_name}' not found", entity_kind="vm", entity_name=console.vm_name)
        _mc._session_linux_user(db, session, vm)
    return members


def set_console_desired_runtime_state(
    db: Database,
    *,
    name: str,
    state: str | DesiredRuntimeState,
) -> None:
    """Set local console intent without loading configuration or touching a VM."""
    try:
        target = DesiredRuntimeState(state)
    except (ValueError, TypeError):
        raise ValidationError("invalid console runtime target", entity_kind="console", entity_name=name) from None
    if target is DesiredRuntimeState.ON_DEMAND:
        raise ValidationError("consoles require a running or stopped target", entity_kind="console", entity_name=name)
    with guarded_console_selection(db, name):
        db.set_console_desired_runtime_state(name, target)


def accept_console_running(
    db: Database,
    *,
    console: ConsoleRow,
    held_native_guard: NativeMutationGuard,
) -> None:
    with native_mutation_guard(db.path, console.vm_name, held=held_native_guard):
        current = _require_console(db, console.name)
        if current != console:
            raise StateError("console definition changed before start acceptance")
        validate_console_definition(db, console)
        db.set_console_desired_runtime_state(console.name, DesiredRuntimeState.RUNNING)


def _presence(target: Transport | None, name: str) -> tuple[ProbeStatus, ProbeStatus]:
    if target is None:
        return ProbeStatus.ABSENT, ProbeStatus.ABSENT
    canonical, staging = _mc._console_runtime_presence(target, name)
    if ProbeStatus.UNKNOWN in {canonical, staging}:
        raise StateError(f"could not determine console '{name}' tmux state", entity_kind="console", entity_name=name)
    return canonical, staging


def prepare_console_reconcile(
    db: Database,
    registry: Registry,
    *,
    console: ConsoleRow,
    vm: VMRow,
    target: Transport,
    held_native_guard: NativeMutationGuard,
    replace_running: bool = False,
) -> PreparedConsoleReconcile:
    """Observe and collect build requirements without changing tmux state."""
    with native_mutation_guard(db.path, console.vm_name, held=held_native_guard):
        current = _require_console(db, console.name)
        if current != console or vm.name != console.vm_name:
            raise StateError("console definition changed before runtime preparation")
        members = validate_console_definition(db, console)
        canonical, staging = _presence(target, console.name)
        if replace_running:
            action = ConsoleAction.REPLACE
        elif canonical is ProbeStatus.PRESENT:
            action = ConsoleAction.CLEANUP_STAGING if staging is ProbeStatus.PRESENT else ConsoleAction.UNCHANGED
        else:
            action = ConsoleAction.BUILD
        targets = (
            tuple(_mc._console_build_secret_targets(db, registry, console=console, vm=vm, members=members))
            if action in {ConsoleAction.BUILD, ConsoleAction.REPLACE}
            else ()
        )
        return PreparedConsoleReconcile(
            db,
            registry,
            console,
            vm,
            target,
            held_native_guard,
            members,
            named_console_template(registry).tmux_layout,
            canonical,
            staging,
            action,
            targets,
        )


def prepare_console_stop(
    db: Database,
    *,
    console: ConsoleRow,
    vm: VMRow,
    target: Transport | None,
    held_native_guard: NativeMutationGuard,
) -> PreparedConsoleStop:
    """Prepare exact teardown; a provider-off VM is represented by no transport."""
    with native_mutation_guard(db.path, console.vm_name, held=held_native_guard):
        if _require_console(db, console.name) != console or vm.name != console.vm_name:
            raise StateError("console definition changed before stop preparation")
        canonical, staging = _presence(target, console.name)
        action = ConsoleAction.STOP if ProbeStatus.PRESENT in {canonical, staging} else ConsoleAction.UNCHANGED
        return PreparedConsoleStop(
            db,
            console,
            vm,
            target,
            held_native_guard,
            tuple(db.list_console_sessions(console.name)),
            canonical,
            staging,
            action,
        )


def _verify_prepared(prepared: PreparedConsoleReconcile | PreparedConsoleStop) -> None:
    current = _require_console(prepared.db, prepared.console.name)
    if (
        replace(current, last_started_at=prepared.console.last_started_at) != prepared.console
        or tuple(prepared.db.list_console_sessions(current.name)) != prepared.members
    ):
        raise StateError("console definition or membership changed after preparation")
    vm = prepared.db.get_vm(prepared.vm.name)
    if vm is None or vm.vm_uuid != prepared.vm.vm_uuid:
        raise StateError("console parent VM changed after preparation")


def execute_console_reconcile(prepared: PreparedConsoleReconcile, *, values: Mapping[str, str]) -> bool:
    """Apply a prepared console action using only supplied workload values."""
    from .attach import _teardown_console_staging

    with native_mutation_guard(prepared.db.path, prepared.vm.name, held=prepared.held_native_guard):
        _verify_prepared(prepared)
        assert prepared.target is not None
        canonical, staging = _presence(prepared.target, prepared.console.name)
        if prepared.action is ConsoleAction.UNCHANGED:
            if canonical is not ProbeStatus.PRESENT or staging is not ProbeStatus.ABSENT:
                raise StateError("console runtime changed after preparation")
            return False
        if prepared.action is ConsoleAction.CLEANUP_STAGING:
            if canonical is not ProbeStatus.PRESENT:
                raise StateError("console canonical runtime disappeared after preparation")
            _teardown_console_staging(prepared.target, prepared.console.name)
            return staging is ProbeStatus.PRESENT
        if canonical is ProbeStatus.PRESENT and prepared.action is ConsoleAction.BUILD:
            if staging is ProbeStatus.PRESENT:
                _teardown_console_staging(prepared.target, prepared.console.name)
                return True
            return False
        if prepared.action is ConsoleAction.REPLACE:
            _mc._teardown_console_tmux(prepared.target, prepared.console.name)
        _mc._build_console_tmux(
            prepared.target,
            prepared.db,
            prepared.registry,
            prepared.console,
            prepared.vm,
            values=values,
            layout=prepared.layout,
            members=prepared.members,
        )
        return True


def execute_console_stop(prepared: PreparedConsoleStop) -> bool:
    """Execute prepared teardown without resolving secrets or starting a VM."""
    with native_mutation_guard(prepared.db.path, prepared.vm.name, held=prepared.held_native_guard):
        _verify_prepared(prepared)
        if prepared.target is None:
            return False
        canonical, staging = _presence(prepared.target, prepared.console.name)
        _mc._teardown_console_tmux(prepared.target, prepared.console.name)
        return ProbeStatus.PRESENT in {canonical, staging}


def observe_prepared_console(prepared: PreparedConsoleReconcile | PreparedConsoleStop) -> ConsoleStatus:
    """Observe the retained exact names without resolving sources or activating."""
    with native_mutation_guard(prepared.db.path, prepared.vm.name, held=prepared.held_native_guard):
        _verify_prepared(prepared)
        canonical, staging = _presence(prepared.target, prepared.console.name)
        return classify_console_status(
            canonical_present=canonical is ProbeStatus.PRESENT,
            staging_present=staging is ProbeStatus.PRESENT,
        )
