"""Read-only artifact routing through actual owners and reusable facet results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agentworks.artifacts.state import CapturedArtifacts, declaration_digest, read_captures
from agentworks.errors import ConfigError, StateError
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.state import read_native_setup
from agentworks.secrets.orchestration import SecretTarget

if TYPE_CHECKING:
    from agentworks.artifacts.application import OwnedArtifactFile
    from agentworks.artifacts.model import ArtifactFacet, ArtifactInput
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
    prepared: tuple[ArtifactInput, ...] | None = None


@dataclass(frozen=True)
class RoutingResult:
    inputs: tuple[ArtifactInput, ...]
    ancestor_files: tuple[OwnedArtifactFile, ...] = ()
    active_facets: tuple[ArtifactFacet, ...] = ()


def inspect_owner_artifacts(
    db: Database,
    registry: Registry,
    inputs: SetupInputs,
    integration_name: str | None = None,
    *,
    inherited: tuple[ArtifactInput, ...] | None = (),
) -> ArtifactOwnerView:
    """Project persisted evidence without fetching or repairing any owner.

    ``inherited=None`` explicitly means an ancestor's route is unavailable. It
    cannot be substituted with empty input to claim this owner's result current.
    """
    captured = None
    record = None
    reason = ""
    capture_status: CaptureStatus = "current"
    try:
        captured = inputs.artifact_snapshot or read_captures(db, inputs.kind, inputs.name).get(inputs.component)
        desired = declaration_digest(registry, inputs.artifacts)
        if captured is None and inputs.artifacts.bundles:
            capture_status, reason = "missing", "Artifact bundles have not been captured for this owner."
        elif captured is not None and captured.declaration != desired:
            capture_status, reason = "stale", "Captured artifacts do not match the owner's current bundle declarations."
    except (ConfigError, StateError):
        capture_status, reason = "unavailable", "The owner's artifact declarations or captured state are unavailable."
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
        except StateError:
            return ArtifactOwnerView(
                inputs, captured, capture_status, "unavailable", "The owner's integration state is unavailable."
            )
    if capture_status != "current":
        return ArtifactOwnerView(inputs, captured, capture_status, capture_status, reason, record)
    if inherited is None:
        return ArtifactOwnerView(
            inputs,
            captured,
            capture_status,
            "unavailable",
            "An ancestor's artifact routing result is unavailable.",
            record,
        )
    local = captured.inputs if captured is not None else ()
    prepared = _unique((*inherited, *local))
    if integration_name is None:
        return ArtifactOwnerView(inputs, captured, capture_status, "current", prepared=prepared)
    block = next((block for block in inputs.activations if block.name == integration_name), None)
    if block is None:
        if record is not None and record.artifact_files:
            return ArtifactOwnerView(
                inputs,
                captured,
                capture_status,
                "retirement",
                "The removed activation still owns artifact files; finish its cleanup first.",
                record,
                prepared,
            )
        return ArtifactOwnerView(inputs, captured, capture_status, "inactive", record=record, prepared=prepared)
    # Plugin/settings-only evidence keeps its existing required/recommended
    # readiness behavior. There is no artifact result to reuse in this case.
    if not prepared and (record is None or not record.artifact_files):
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
    if not record.complete or record.pending_cleanup:
        return ArtifactOwnerView(
            inputs,
            captured,
            capture_status,
            "incomplete",
            "The activated facet has unfinished artifact application or cleanup.",
            record,
            prepared,
        )
    try:
        declaration = inputs.declaration(block)
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
    if declaration != record.declaration or tuple(item.identity for item in prepared) != record.artifact_inputs:
        return ArtifactOwnerView(
            inputs,
            captured,
            capture_status,
            "stale",
            "The activated facet's config, environment or prepared artifacts have changed.",
            record,
            prepared,
        )
    allowed: tuple[ArtifactFacet, ...] = ("user", "workspace", "session") if inputs.component == "vm" else ("session",)
    if inputs.component == "session":
        allowed = ()
    if any(item.destination not in allowed for item in record.deferred):
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


def deferred_inputs(view: ArtifactOwnerView, destination: ArtifactFacet) -> tuple[ArtifactInput, ...] | None:
    """Project one route, preserving input order; unknown evidence stays unknown."""
    if view.status not in ("current", "inactive") or view.prepared is None:
        return None
    if view.status == "inactive":
        return view.prepared if destination == "session" else ()
    if view.record is None:
        return ()
    identities = {item.input_id for item in view.record.deferred if item.destination == destination}
    return tuple(item for item in view.prepared if item.identity in identities)


def setup_artifacts(
    db: Database, registry: Registry, inputs: SetupInputs, vm: VMRow, integration_name: str
) -> tuple[ArtifactInput, ...]:
    """Prepare a facet's local capture plus only its applicable VM route."""
    if inputs.kind == "vm" and inputs.name != vm.name:
        raise StateError("artifact setup requires the actual owning VM")
    local = _require(inspect_owner_artifacts(db, registry, inputs)).prepared or ()
    if inputs.component == "vm":
        return local
    if inputs.component not in ("admin", "agent", "workspace"):
        raise StateError("artifact setup requires a VM, user or workspace facet")
    ancestor = _vm_inputs(db, registry, vm)
    view = _require(inspect_owner_artifacts(db, registry, ancestor, integration_name))
    inherited = deferred_inputs(view, inputs.facet)
    assert inherited is not None
    return _unique((*inherited, *local))


def session_artifacts(
    db: Database,
    registry: Registry,
    vm: VMRow,
    workspace: WorkspaceRow,
    agent_name: str | None,
    integration_name: str,
    local: tuple[ArtifactInput, ...],
) -> RoutingResult:
    """Join one actual owner diamond without consuming or rerunning ancestors."""
    if workspace.vm_name != vm.name:
        raise StateError("artifact routing requires the session's actual workspace on its VM")
    vm_inputs = _vm_inputs(db, registry, vm)
    user_inputs = _user_inputs(db, registry, vm, vm_inputs, agent_name)
    workspace_inputs = _workspace_inputs(db, registry, workspace, vm_inputs)
    vm_view = _require(inspect_owner_artifacts(db, registry, vm_inputs, integration_name))
    user_route = deferred_inputs(vm_view, "user")
    workspace_route = deferred_inputs(vm_view, "workspace")
    direct = deferred_inputs(vm_view, "session")
    assert user_route is not None and workspace_route is not None and direct is not None
    user_view = _require(inspect_owner_artifacts(db, registry, user_inputs, integration_name, inherited=user_route))
    workspace_view = _require(
        inspect_owner_artifacts(db, registry, workspace_inputs, integration_name, inherited=workspace_route)
    )
    user_deferred = deferred_inputs(user_view, "session")
    workspace_deferred = deferred_inputs(workspace_view, "session")
    assert user_deferred is not None and workspace_deferred is not None
    selected = _unique((*direct, *user_deferred, *workspace_deferred, *local))
    selected_ids = {item.identity for item in selected}
    # Restore declaration order even when VM entries took different diamond paths.
    ordered = _unique((*_local(vm_view), *_local(user_view), *_local(workspace_view), *local))
    result = tuple(item for item in ordered if item.identity in selected_ids)
    if len(result) != len(selected):
        raise StateError("artifact routing encountered input outside its actual owner graph")
    files: list[OwnedArtifactFile] = []
    active: list[ArtifactFacet] = []
    for view in (vm_view, user_view, workspace_view):
        if view.status == "current" and view.record is not None and (view.prepared or view.record.artifact_files):
            files.extend(view.record.artifact_files)
            active.append(view.owner.facet)
    return RoutingResult(result, tuple(files), tuple(active))


def _require(view: ArtifactOwnerView) -> ArtifactOwnerView:
    if view.status not in ("current", "inactive"):
        if view.owner.component == "workspace":
            remedy = f"Recreate workspace '{view.owner.name}' with its current setup."
        elif view.owner.component == "agent":
            remedy = f"Run 'agw agent reinit {view.owner.name}'."
        else:
            remedy = f"Run 'agw vm reinit {view.owner.name}'."
        raise StateError(view.reason, entity_kind=view.owner.kind, entity_name=view.owner.name, hint=remedy)
    return view


def _local(view: ArtifactOwnerView) -> tuple[ArtifactInput, ...]:
    return view.captured.inputs if view.captured is not None else ()


def _unique(inputs: tuple[ArtifactInput, ...]) -> tuple[ArtifactInput, ...]:
    if len({item.identity for item in inputs}) != len(inputs):
        raise StateError("artifact routing delivered the same input along more than one path")
    return inputs


def _vm_inputs(db: Database, registry: Registry, vm: VMRow) -> SetupInputs:
    from agentworks.vms.templates import resolve_live_template

    template = resolve_live_template(db, registry, vm.name, vm.template)
    return SetupInputs(
        "vm",
        vm.name,
        "vm",
        tuple(template.harness_integrations),
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
            tuple(admin.harness_integrations),
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
        tuple(template.harness_integrations),
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
        tuple(template.harness_integrations),
        SecretTarget(vm=ancestor.target.vm, workspace=template.env),
        artifacts=template.artifacts,
    )
