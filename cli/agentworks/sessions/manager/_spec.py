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
    from agentworks.resources.live import LiveResource
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
) -> LiveResource:
    """Validate the session as ``overlay`` would declare it; return it as it publishes."""
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
    return pending


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
    """Own validation and restart consent, then hand the layer to the batch, which accepts it with the intent.

    The restart consent probe and its confirmation run with nothing locked.
    This command takes no lock of its own: the batch replaces the layer
    under its family's brief set at intent acceptance, after it observes the
    session again, so a session whose status changed since the confirmation
    refuses there before the layer is replaced.
    """
    from agentworks.instance_specs import parse_instance_spec, render_overlay_outcome
    from agentworks.recheck import require_unchanged
    from agentworks.runtime_state import run_session_batch
    from agentworks.runtime_state._models import SessionDeclaration
    from agentworks.secrets.policy import require_exact_tty_interaction_policy

    from ._lifecycle import _confirm_running_restart

    require_exact_tty_interaction_policy(interaction)
    overlay = parse_instance_spec("session", spec)
    selected = _mgr._require_session(db, name)
    workspace = _mgr._require_workspace(db, selected.workspace_name)
    vm = _mgr._require_vm_for_workspace(db, workspace)

    def observed_running(session: SessionRow) -> bool:
        status = _mgr.observe_session_statuses([session], db=db, config=config, include_legacy=True)
        return status.get(name, SessionStatus.UNKNOWN) is SessionStatus.RUNNING

    session = _current_session(db, selected, vm)
    pending = _validate_candidate(db, config, session, overlay, intent=intent)
    authorized = False
    if operation == "restart":
        authorized = observed_running(session)
        _confirm_running_restart([name] if authorized else [], yes=yes, interaction=interaction)
    outcomes: list[OverlayOutcome] = []

    def accept(current: SessionRow, status: SessionStatus) -> None:
        # Against the session and status as this command read them before its consent, not as the batch read them.
        if operation == "restart":
            require_unchanged("session", name, authorized, status is SessionStatus.RUNNING)
        with db.transaction():
            outcomes.append(_persist_spec(db, _current_session(db, session, vm), overlay))

    try:
        run_session_batch(
            db,
            config,
            [session],
            operation=operation,
            force=force,
            intent=intent,
            running_restart_authorized=frozenset({name}) if authorized else frozenset(),
            interaction=interaction,
            declarations={name: SessionDeclaration(pending, accept)},
        )
    finally:
        for outcome in outcomes:
            render_overlay_outcome(outcome)
