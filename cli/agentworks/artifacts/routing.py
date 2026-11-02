"""Read-only artifact routing through actual owners and reusable facet results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from agentworks import output
from agentworks.artifacts.model import ALLOWED_DEFERRALS, ArtifactInputs
from agentworks.artifacts.state import (
    CapturedArtifacts,
    UnsupportedArtifactCaptureVersionError,
    declaration_digest,
    read_captures,
)
from agentworks.errors import ConfigError, StateError
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.state import UnsupportedNativeSetupVersionError, read_native_setup
from agentworks.secrets.orchestration import SecretTarget

if TYPE_CHECKING:
    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.artifacts.model import ArtifactFacet, ArtifactGroup, ArtifactOwner
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.harness_setup.model import SetupRecord
    from agentworks.resources.registry import Registry

CaptureStatus = Literal["current", "missing", "stale", "unavailable"]
ArtifactStatus = Literal["current", "inactive", "missing", "stale", "incomplete", "retirement", "unavailable"]


@dataclass(frozen=True)
class ArtifactOwnerView:
    """Declaration, captured content and applied evidence remain distinguishable."""

    owner: SetupInputs
    captured: CapturedArtifacts | None
    capture_status: CaptureStatus
    status: ArtifactStatus
    reason: str = ""
    record: SetupRecord | None = None
    prepared: ArtifactInputs | None = None
    repair_hint: str | None = None

    @property
    def diagnostic(self) -> str:
        return " ".join(value for value in (self.reason, self.repair_hint) if value)


@dataclass(frozen=True)
class RoutingResult:
    inputs: ArtifactInputs
    ancestor_files: tuple[OwnedArtifactFile, ...] = ()
    active_facets: tuple[ArtifactFacet, ...] = ()


def inspect_owner_artifacts(
    db: Database,
    registry: Registry,
    inputs: SetupInputs,
    integration_name: str | None = None,
    *,
    inherited: Mapping[ArtifactOwner, ArtifactGroup] = MappingProxyType({}),
) -> ArtifactOwnerView:
    """Project persisted evidence and known inputs without fetching or repairing owners."""
    captured = None
    record = None
    reason = ""
    repair_hint = None
    capture_status: CaptureStatus = "current"
    try:
        captured = inputs.artifact_snapshot or read_captures(db, inputs.kind, inputs.name).get(inputs.component)
        desired = declaration_digest(registry, inputs.artifacts)
        if captured is None and inputs.artifacts.bundles:
            capture_status, reason = "missing", "Artifact bundles have not been captured for this owner."
        elif captured is not None and captured.declaration != desired:
            capture_status, reason = "stale", "Captured artifacts do not match the owner's current bundle declarations."
    except UnsupportedArtifactCaptureVersionError as error:
        capture_status, reason, repair_hint = "unavailable", f"{error}.", error.hint
    except (ConfigError, StateError, KeyError):
        capture_status, reason = "unavailable", "The owner's artifact declarations or captured state are unavailable."
    prepared = ArtifactInputs(local=captured.inputs if captured is not None else None, deferred=inherited)
    if integration_name is not None:
        try:
            record = next(
                (
                    item
                    for item in read_native_setup(db, inputs.kind, inputs.name).records
                    if item.component == inputs.component and item.integration == integration_name
                ),
                None,
            )
        except StateError as error:
            return ArtifactOwnerView(
                inputs,
                captured,
                capture_status,
                "unavailable",
                f"{error}."
                if isinstance(error, UnsupportedNativeSetupVersionError)
                else "The owner's integration state is unavailable.",
                prepared=prepared,
                repair_hint=error.hint if isinstance(error, UnsupportedNativeSetupVersionError) else None,
            )
    block = inputs.activations.get(integration_name) if integration_name is not None else None
    recorded_details = ""
    if record is not None:
        if record.incomplete_reasons:
            recorded_details += " Recorded incomplete entries: " + _brief(record.incomplete_reasons) + "."
        if record.skipped:
            recorded_details += (
                " Skipped destinations: "
                + _brief(tuple(f"{item.path}: {item.reason}" for item in record.skipped))
                + "."
            )
    if inputs.component == "session" and integration_name is not None and block is None:
        if record is not None and record.artifact_files:
            return ArtifactOwnerView(
                inputs,
                captured,
                capture_status,
                "retirement",
                "The removed activation still owns artifact files." + recorded_details,
                record,
            )
        return ArtifactOwnerView(inputs, captured, capture_status, "inactive", record=record)
    if capture_status != "current":
        return ArtifactOwnerView(
            inputs, captured, capture_status, capture_status, reason, record, prepared, repair_hint=repair_hint
        )
    if integration_name is None:
        return ArtifactOwnerView(inputs, captured, capture_status, "current", prepared=prepared)
    if block is None:
        if record is not None and (record.artifact_files or record.claims or record.pending_cleanup):
            return ArtifactOwnerView(
                inputs,
                captured,
                capture_status,
                "retirement",
                "The removed activation retains native setup or artifact cleanup evidence." + recorded_details,
                record,
                prepared,
            )
        return ArtifactOwnerView(inputs, captured, capture_status, "inactive", record=record, prepared=prepared)
    if record is not None and (not record.complete or record.pending_cleanup or record.skipped):
        reason = (
            "The activated facet still has native setup or artifact cleanup pending."
            if record.pending_cleanup
            else "The activated facet's previous setup did not complete all declared entries."
        )
        reason += recorded_details
        return ArtifactOwnerView(
            inputs,
            captured,
            capture_status,
            "incomplete",
            reason,
            record,
            prepared,
        )
    # An optional native-only facet needs no fabricated artifact result, but its
    # incomplete native setup above remains actionable even without artifact inputs.
    if not prepared and (record is None or not (record.artifact_files or record.artifact_inputs)):
        return ArtifactOwnerView(inputs, captured, capture_status, "current", record=record, prepared=prepared)
    if record is None or record.artifact_inputs is None:
        return ArtifactOwnerView(
            inputs,
            captured,
            capture_status,
            "missing",
            "The activated facet has no recorded artifact handling result.",
            record,
            prepared,
        )
    try:
        declaration = inputs.declaration(integration_name, block)
    except ConfigError:
        return ArtifactOwnerView(
            inputs,
            captured,
            capture_status,
            "unavailable",
            "The activated facet's current configuration is unavailable.",
            record,
            prepared,
        )
    current_ids = tuple(item.identity for item in prepared.items())
    if declaration != record.declaration or current_ids != record.artifact_inputs:
        differences = _declaration_changes(record.declaration, declaration) if declaration != record.declaration else []
        if current_ids != record.artifact_inputs:
            changed = tuple(
                f"{item.content.type.value} {item.content.name}"
                for item in prepared.items()
                if item.identity not in record.artifact_inputs
            )
            differences.append(
                f"prepared artifacts changed: {_brief(changed)}"
                if changed
                else "prepared artifact order or removals changed"
            )
        return ArtifactOwnerView(
            inputs,
            captured,
            capture_status,
            "stale",
            "; ".join(differences) + ".",
            record,
            prepared,
        )
    if any(item.destination not in ALLOWED_DEFERRALS[inputs.facet] for item in record.deferred):
        return ArtifactOwnerView(
            inputs,
            captured,
            capture_status,
            "unavailable",
            "The recorded artifact result contains an invalid deferral route.",
            record,
            prepared,
        )
    return ArtifactOwnerView(inputs, captured, capture_status, "current", record=record, prepared=prepared)


def _brief(names: tuple[str, ...]) -> str:
    """Keep drift diagnostics bounded without exposing values or artifact bodies."""
    return ", ".join(names if len(names) <= 3 else (*names[:2], "..."))


def _changed_keys(before: object, after: object) -> tuple[str, ...]:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return ()
    missing = object()
    keys = before.keys() | after.keys()
    return tuple(sorted(key for key in keys if before.get(key, missing) != after.get(key, missing)))


def _declaration_changes(before: Mapping[str, object], after: Mapping[str, object]) -> list[str]:
    """Name only changed declaration keys; persisted values never enter diagnostics."""
    changes: list[str] = []
    if before.get("config") != after.get("config"):
        keys = _changed_keys(before.get("config"), after.get("config"))
        changes.append(f"integration config changed: {_brief(keys)}" if keys else "integration config changed")
    old_env, new_env = before.get("env"), after.get("env")
    if old_env != new_env:
        if isinstance(old_env, Mapping) and isinstance(new_env, Mapping):
            for scope in sorted(old_env.keys() | new_env.keys()):
                if old_env.get(scope) == new_env.get(scope):
                    continue
                keys = _changed_keys(old_env.get(scope), new_env.get(scope))
                changes.append(
                    f"{scope} environment changed: {_brief(keys)}" if keys else f"{scope} environment changed"
                )
        else:
            changes.append("declared environment changed")
    return changes or ["setup declaration changed"]


def inactive_destination(facet: ArtifactFacet) -> ArtifactFacet:
    """Route inactive VM facets through the user; inner facets pass to session."""
    return "user" if facet == "vm" else "session"


def available_deferred_inputs(
    view: ArtifactOwnerView, destination: ArtifactFacet, integration_name: str
) -> dict[ArtifactOwner, ArtifactGroup]:
    """Project only known captured routes, independently of their freshness status.

    An activated facet with unavailable handling evidence cannot acknowledge or
    invent a deferral. Its readable explicit deferrals remain usable after drift.
    """
    if view.prepared is None:
        return {}
    if integration_name not in view.owner.activations:
        return (
            {group.owner: group for group in view.prepared.groups() if group}
            if destination == inactive_destination(view.owner.facet)
            else {}
        )
    if view.record is None or destination not in ALLOWED_DEFERRALS[view.owner.facet]:
        return {}
    identities = {item.input_id for item in view.record.deferred if item.destination == destination}
    selected = (group.select(identities) for group in view.prepared.groups())
    return {group.owner: group for group in selected if group}


def report_ancestor(view: ArtifactOwnerView, integration_name: str) -> None:
    """Explain upstream repair without treating it as a descendant prerequisite."""
    if view.status not in ("current", "inactive"):
        owner = view.owner
        output.warn(
            f"Setup via {integration_name} at {owner.component}/{owner.name}: {view.reason} "
            f"{owner_remediation(view)} Continuing with available captured routes and existing placement."
        )


def setup_artifacts(
    db: Database, registry: Registry, inputs: SetupInputs, vm: VMRow, integration_name: str
) -> ArtifactInputs:
    """Prepare local capture plus available VM routes without repairing ancestors."""
    if inputs.kind == "vm" and inputs.name != vm.name:
        raise StateError("artifact setup requires the actual owning VM")
    prepared = _require(inspect_owner_artifacts(db, registry, inputs)).prepared
    assert prepared is not None
    if inputs.component == "vm":
        return prepared
    if inputs.component not in ("admin", "agent", "workspace"):
        raise StateError("artifact setup requires a VM, user or workspace facet")
    ancestor = _vm_inputs(db, registry, vm)
    view = inspect_owner_artifacts(db, registry, ancestor, integration_name)
    report_ancestor(view, integration_name)
    return ArtifactInputs(
        local=prepared.local, deferred=available_deferred_inputs(view, inputs.facet, integration_name)
    )


def session_artifacts(
    db: Database,
    registry: Registry,
    vm: VMRow,
    workspace: WorkspaceRow,
    agent_name: str | None,
    integration_name: str,
    local: ArtifactGroup,
) -> RoutingResult:
    """Join known owner routes without requiring or rerunning ancestor setup."""
    vm_view, user_view, workspace_view = session_ancestor_views(
        db, registry, vm, workspace, agent_name, integration_name
    )
    ancestors = (vm_view, user_view, workspace_view)
    for view in ancestors:
        report_ancestor(view, integration_name)
    inherited = session_inherited_inputs(*ancestors, integration_name=integration_name, report=True)
    result = ArtifactInputs(local=local, deferred=inherited)
    files: list[OwnedArtifactFile] = []
    active: list[ArtifactFacet] = []
    for view in ancestors:
        if view.record is not None and (view.prepared or view.record.artifact_files):
            skipped_paths = {item.path for item in view.record.skipped}
            files.extend(item for item in view.record.artifact_files if item.path not in skipped_paths)
            if view.status == "current":
                active.append(view.owner.facet)
    return RoutingResult(result, tuple(files), tuple(active))


def session_ancestor_views(
    db: Database,
    registry: Registry,
    vm: VMRow,
    workspace: WorkspaceRow,
    agent_name: str | None,
    integration_name: str,
) -> tuple[ArtifactOwnerView, ArtifactOwnerView, ArtifactOwnerView]:
    """Read ancestor facts without making their freshness a descendant gate."""
    if workspace.vm_name != vm.name:
        raise StateError("artifact routing requires the session's actual workspace on its VM")
    vm_inputs = _vm_inputs(db, registry, vm)
    vm_view = inspect_owner_artifacts(db, registry, vm_inputs, integration_name)
    user_inputs = _user_inputs(db, registry, vm, vm_inputs, agent_name)
    user_view = inspect_owner_artifacts(
        db,
        registry,
        user_inputs,
        integration_name,
        inherited=available_deferred_inputs(vm_view, "user", integration_name),
    )
    workspace_inputs = _workspace_inputs(db, registry, workspace, vm_inputs)
    workspace_view = inspect_owner_artifacts(
        db,
        registry,
        workspace_inputs,
        integration_name,
        inherited=available_deferred_inputs(vm_view, "workspace", integration_name),
    )
    return vm_view, user_view, workspace_view


def session_inherited_inputs(
    vm: ArtifactOwnerView,
    user: ArtifactOwnerView,
    workspace: ArtifactOwnerView,
    *,
    integration_name: str,
    report: bool = False,
) -> dict[ArtifactOwner, ArtifactGroup]:
    """Union available deferrals once in original owner and declaration order."""
    ancestors = (vm, user, workspace)
    routes = []
    for view in ancestors:
        if integration_name in view.owner.activations:
            # A recorded result belongs to this facet's original invocation.
            # Its upstream routes may have changed since then.
            identities = (
                ()
                if view.record is None
                else tuple(item.input_id for item in view.record.deferred if item.destination == "session")
            )
        else:
            identities = tuple(
                item.identity
                for group in available_deferred_inputs(view, "session", integration_name).values()
                for item in group.items()
            )
        routes.append(identities)
    selected = [identity for route in routes for identity in route]
    if report and len(set(selected)) != len(selected):
        output.warn(f"Repeated artifact routes via {integration_name} were combined once; refresh the owning facets.")
    selected_ids = set(selected)
    result = {}
    for view in ancestors:
        if view.captured is not None:
            group = view.captured.inputs.select(selected_ids)
            if group:
                result[group.owner] = group
    resolved = {item.identity for group in result.values() for item in group.items()}
    if report:
        for view, route in zip(ancestors, routes, strict=True):
            unresolved = tuple(identity[:12] for identity in route if identity not in resolved)
            if unresolved:
                output.warn(
                    f"Artifact routes via {integration_name} at {view.owner.component}/{view.owner.name} "
                    f"have no readable source in this owner graph: {_brief(unresolved)}. "
                    f"{owner_remediation(view)} Unknown content was omitted."
                )
    return result


def owner_remediation(view: ArtifactOwnerView) -> str:
    if view.repair_hint is not None:
        return view.repair_hint
    conflict = (
        "Resolve the recorded entry or destination conflicts first. "
        if view.record is not None and (view.record.incomplete_reasons or view.record.skipped)
        else ""
    )
    if view.owner.component == "workspace":
        return (
            f"{conflict}Recreate workspace '{view.owner.name}' to apply changed setup; "
            "workspace reinit is not supported."
        )
    if view.owner.component == "agent":
        return f"{conflict}Run 'agw agent reinit {view.owner.name}'."
    return f"{conflict}Run 'agw vm reinit {view.owner.name}'."


def _require(view: ArtifactOwnerView) -> ArtifactOwnerView:
    if view.status not in ("current", "inactive"):
        raise StateError(
            view.reason, entity_kind=view.owner.kind, entity_name=view.owner.name, hint=owner_remediation(view)
        )
    return view


def _vm_inputs(db: Database, registry: Registry, vm: VMRow) -> SetupInputs:
    from agentworks.vms.templates import resolve_live_template

    template = resolve_live_template(db, registry, vm.name, vm.template)
    return SetupInputs(
        "vm",
        vm.name,
        "vm",
        template.harness_integrations,
        SecretTarget(vm=template.env),
        artifacts=template.artifacts,
    )


def _user_inputs(
    db: Database, registry: Registry, vm: VMRow, ancestor: SetupInputs, agent_name: str | None
) -> SetupInputs:
    if agent_name is None:
        from agentworks.vms.admin_templates import resolve_live_template as resolve_admin

        admin = resolve_admin(db, registry, vm.name, vm.admin_template)
        return SetupInputs(
            "vm",
            vm.name,
            "admin",
            admin.active_harness_integrations,
            SecretTarget(vm=ancestor.target.vm, admin=admin.env),
            artifacts=admin.artifacts,
        )
    from agentworks.agents.templates import resolve_live_template as resolve_agent

    agent = db.get_agent(agent_name)
    if agent is None or agent.vm_name != vm.name:
        raise StateError("artifact routing requires the session's actual agent on its VM")
    template = resolve_agent(db, registry, agent.name, agent.template)
    return SetupInputs(
        "agent",
        agent.name,
        "agent",
        template.harness_integrations,
        SecretTarget(vm=ancestor.target.vm, agent=template.env),
        artifacts=template.artifacts,
    )


def _workspace_inputs(db: Database, registry: Registry, workspace: WorkspaceRow, ancestor: SetupInputs) -> SetupInputs:
    from agentworks.workspaces.templates import resolve_live_template

    template = resolve_live_template(db, registry, workspace.name, workspace.template)
    return SetupInputs(
        "workspace",
        workspace.name,
        "workspace",
        template.harness_integrations,
        SecretTarget(vm=ancestor.target.vm, workspace=template.env),
        artifacts=template.artifacts,
    )
