"""Validate and accept a replacement session declaration before launching it."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import agentworks.sessions.manager as _mgr
from agentworks.db import SessionRuntimeSnapshot, SessionStatus
from agentworks.errors import StateError
from agentworks.instance_specs import OverlayDisposition, OverlayOutcome

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from agentworks.config import Config
    from agentworks.db import Database, SessionRow, VMRow
    from agentworks.instance_specs import InstanceOverlay
    from agentworks.runtime_state._models import SessionOperation
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.sessions.template import SessionTemplate


def _current_session(db: Database, expected: SessionRow, vm: VMRow) -> SessionRow:
    """Refuse replacement of the selected session, route, or runtime while preparing."""
    current = _mgr._require_session(db, expected.name)
    workspace = _mgr._require_workspace(db, current.workspace_name)
    current_vm = _mgr._require_vm_for_workspace(db, workspace)
    if (
        SessionRuntimeSnapshot.from_session(current) != SessionRuntimeSnapshot.from_session(expected)
        or current.workspace_name != expected.workspace_name
        or current.agent_name != expected.agent_name
        or current.template != expected.template
        or current_vm.name != vm.name
        or current_vm.vm_uuid != vm.vm_uuid
    ):
        raise StateError(
            f"session '{expected.name}' or its parent changed while the operation was preparing",
            entity_kind="session",
            entity_name=expected.name,
            hint="Retry against the current session.",
        )
    return current


def _validate_candidate(
    db: Database,
    config: Config,
    session: SessionRow,
    overlay: InstanceOverlay[BaseModel],
    *,
    intent: HarnessLaunchIntent,
) -> None:
    from agentworks.bootstrap import load_request_registry
    from agentworks.capabilities.harness_integration import ensure_harness_integration_enabled, harness_integration_for
    from agentworks.instance_specs import ensure_effective_references_enabled
    from agentworks.resources.access import ensure_recipe_enabled
    from agentworks.resources.live_publish import project_session_live_resource
    from agentworks.sessions.templates import resolve_template_with_provenance

    declarations = load_request_registry(config, include_live_resources=False, probe_host_readiness=False)
    layered = resolve_template_with_provenance(
        declarations,
        session.template,
        overlay=cast("SessionTemplate", overlay.declaration) if overlay.payload.value else None,
        instance_name=session.name,
    )
    pending = project_session_live_resource(
        name=session.name,
        workspace_name=session.workspace_name,
        agent_name=session.agent_name,
        template_name=session.template,
        layered=layered,
    )
    registry = load_request_registry(
        config,
        live_database=db,
        probe_host_readiness=False,
        pending_publishers=(lambda target: target.add_live(pending),),
    )
    ensure_effective_references_enabled(registry, pending.outbound)
    ensure_recipe_enabled(registry, "session-template", session.template)
    ensure_harness_integration_enabled(registry, layered.value.harness_integration)
    harness_integration_for(layered.value.harness_integration).validate_launch_config(
        layered.value.harness_integration_config, intent=intent
    )


def _persist_spec(db: Database, session: SessionRow, overlay: InstanceOverlay[BaseModel]) -> OverlayOutcome:
    """Replace the accepted desired layer without decoding unsupported prior state."""
    prior = db.instance_state.get_desired_overlay("session", session.name)
    if not overlay.payload.value:
        db.instance_state.clear_desired_overlay("session", session.name)
        return OverlayOutcome(
            OverlayDisposition.EXPLICITLY_ABSENT if prior is None else OverlayDisposition.CLEARED,
            # An older or newer stored payload may be opaque; do not render its contents.
            (),
        )
    db.instance_state.put_desired_overlay("session", session.name, overlay.payload)
    return OverlayOutcome(OverlayDisposition.SET if prior is None else OverlayDisposition.REPLACED, overlay.fields)


def launch_with_spec(
    db: Database,
    config: Config,
    *,
    name: str,
    spec: str,
    operation: SessionOperation,
    force: bool,
    intent: HarnessLaunchIntent,
    yes: bool,
    interaction: TtyInteractionPolicy,
) -> None:
    """Own validation, restart consent, and replacement under the launch's family guard."""
    from agentworks.harness_setup.locking import native_mutation_guard
    from agentworks.instance_specs import parse_instance_spec, report_overlay_outcome
    from agentworks.runtime_state import run_session_batch
    from agentworks.secrets.policy import require_exact_tty_interaction_policy

    from ._lifecycle import _confirm_running_restart

    require_exact_tty_interaction_policy(interaction)
    overlay = parse_instance_spec("session", spec)
    selected = _mgr._require_session(db, name)
    workspace = _mgr._require_workspace(db, selected.workspace_name)
    vm = _mgr._require_vm_for_workspace(db, workspace)
    with native_mutation_guard(db.path, vm.name) as guard:
        session = _current_session(db, selected, vm)
        _validate_candidate(db, config, session, overlay, intent=intent)
        authorized = False
        if operation == "restart":
            status = _mgr.observe_session_statuses([session], db=db, config=config, include_legacy=True)
            authorized = status.get(name, SessionStatus.UNKNOWN) is SessionStatus.RUNNING
            _confirm_running_restart([name] if authorized else [], yes=yes, interaction=interaction)
        with db.transaction():
            session = _current_session(db, session, vm)
            outcome = _persist_spec(db, session, overlay)
        with report_overlay_outcome(outcome):
            run_session_batch(
                db,
                config,
                [session],
                operation=operation,
                force=force,
                intent=intent,
                running_restart_authorized=frozenset({name}) if authorized else frozenset(),
                interaction=interaction,
                held_native_guard=guard,
            )
