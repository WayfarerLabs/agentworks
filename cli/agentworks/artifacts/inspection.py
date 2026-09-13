"""Read-only, metadata-only artifact inspection for an owner's actual ancestry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.artifacts.bundle import resolve_bundle
from agentworks.artifacts.model import ArtifactType
from agentworks.errors import NotFoundError, StateError, ValidationError
from agentworks.harness_setup.inputs import SetupInputs
from agentworks.harness_setup.state import read_native_setup
from agentworks.schema import CapabilityBlock
from agentworks.secrets.orchestration import SecretTarget

if TYPE_CHECKING:
    from agentworks.artifacts.declarations import ArtifactsConfig, ArtifactSpec
    from agentworks.artifacts.model import (
        ArtifactComponent,
        ArtifactFacet,
        ArtifactGroup,
        ArtifactOwner,
        ArtifactReplacement,
    )
    from agentworks.artifacts.routing import ArtifactOwnerView
    from agentworks.db import AgentRow, Database, SessionRow, VMRow, WorkspaceRow
    from agentworks.machine_output import JsonObject
    from agentworks.resources.registry import Registry


@dataclass(frozen=True)
class ArtifactContext:
    vm: VMRow
    workspace: WorkspaceRow | None = None
    agent: AgentRow | None = None
    session: SessionRow | None = None
    admin: bool = False


def resolve_context(
    db: Database,
    *,
    vm_name: str | None = None,
    admin: bool = False,
    agent_name: str | None = None,
    workspace_name: str | None = None,
    session_name: str | None = None,
) -> ArtifactContext:
    """Reject selectors that do not describe one actual owner and its ancestors."""
    if not any((vm_name, agent_name, workspace_name, session_name)):
        raise ValidationError("Select an artifact owner with --vm, --agent, --workspace, or --session.")
    if admin and agent_name is not None:
        raise ValidationError("--admin and --agent select different users.")
    if session_name is None and workspace_name is not None and (agent_name is not None or admin):
        raise ValidationError("A workspace has no user ancestor; select a session to inspect their actual composition.")
    session = db.get_session(session_name) if session_name is not None else None
    if session_name is not None and session is None:
        raise NotFoundError("session not found", entity_kind="session", entity_name=session_name)
    if session is not None:
        if workspace_name is not None and workspace_name != session.workspace_name:
            raise ValidationError("The selected workspace is not the session's workspace.")
        if agent_name is not None and agent_name != session.agent_name:
            raise ValidationError("The selected agent is not the session's user.")
        if admin and session.agent_name is not None:
            raise ValidationError("The selected session runs as an agent, not the VM admin.")
        workspace_name = session.workspace_name
        agent_name = session.agent_name
        admin = session.agent_name is None
    workspace = db.get_workspace(workspace_name) if workspace_name is not None else None
    if workspace_name is not None and workspace is None:
        raise NotFoundError("workspace not found", entity_kind="workspace", entity_name=workspace_name)
    agent = db.get_agent(agent_name) if agent_name is not None else None
    if agent_name is not None and agent is None:
        raise NotFoundError("agent not found", entity_kind="agent", entity_name=agent_name)
    inferred = [owner.vm_name for owner in (workspace, agent) if owner is not None]
    if vm_name is not None:
        inferred.append(vm_name)
    if len(set(inferred)) != 1:
        raise ValidationError("The selected resources do not belong to one VM.")
    vm = db.get_vm(inferred[0])
    if vm is None:
        raise NotFoundError("VM not found", entity_kind="vm", entity_name=inferred[0])
    return ArtifactContext(vm, workspace, agent, session, admin)


@dataclass(frozen=True)
class ArtifactMetadata:
    bundle: str
    entry: str
    type: str
    declared: bool
    input_id: str | None = None
    origin_id: str | None = None
    native_name: str | None = None
    source: str | None = None
    requested_ref: str | None = None
    revision: str | None = None
    selected_path: str | None = None
    replacements: tuple[ArtifactReplacement, ...] = ()


@dataclass(frozen=True)
class DeferralMetadata:
    input_id: str
    destination: str
    reason: str


@dataclass(frozen=True)
class PlacementMetadata:
    path: str
    native_identity: str | None
    origins: tuple[str, ...]


@dataclass(frozen=True)
class IntegrationMetadata:
    name: str
    activated: bool
    status: str
    reason: str
    recorded_handled: tuple[str, ...] = ()
    recorded_deferred: tuple[DeferralMetadata, ...] = ()
    placements: tuple[PlacementMetadata, ...] = ()
    passthrough_inputs: tuple[str, ...] = ()
    passthrough_destination: ArtifactFacet | None = None


@dataclass(frozen=True)
class OwnerMetadata:
    scope: ArtifactComponent
    resource_kind: str
    resource_name: str
    facet: str
    bundles: tuple[str, ...]
    capture_status: str
    artifacts: tuple[ArtifactMetadata, ...]
    integrations: tuple[IntegrationMetadata, ...]
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactInspection:
    owners: tuple[OwnerMetadata, ...]


def _owner_inputs(db: Database, registry: Registry, context: ArtifactContext) -> tuple[SetupInputs, ...]:
    """Resolve declarations only; lifecycle preparation would acquire sources."""
    from agentworks.agents.templates import resolve_live_template as agent_template
    from agentworks.sessions.templates import resolve_live_template as session_template
    from agentworks.vms.admin_templates import resolve_live_template as admin_template
    from agentworks.vms.templates import resolve_live_template as vm_template
    from agentworks.workspaces.templates import resolve_live_template as workspace_template

    vm = vm_template(db, registry, context.vm.name, context.vm.template)
    owners = [
        SetupInputs("vm", context.vm.name, "vm", tuple(vm.harness_integrations), SecretTarget(vm=vm.env), vm.artifacts)
    ]
    user_env, admin_env, workspace_env = None, None, None
    if context.admin:
        user = admin_template(db, registry, context.vm.name, context.vm.admin_template)
        admin_env = user.env
        owners.append(
            SetupInputs(
                "vm",
                context.vm.name,
                "admin",
                tuple(user.harness_integrations),
                SecretTarget(vm=vm.env, admin=user.env),
                user.artifacts,
            )
        )
    elif context.agent is not None:
        agent = agent_template(db, registry, context.agent.name, context.agent.template)
        user_env = agent.env
        owners.append(
            SetupInputs(
                "agent",
                context.agent.name,
                "agent",
                tuple(agent.harness_integrations),
                SecretTarget(vm=vm.env, agent=agent.env),
                agent.artifacts,
            )
        )
    if context.workspace is not None:
        workspace = workspace_template(db, registry, context.workspace.name, context.workspace.template)
        workspace_env = workspace.env
        owners.append(
            SetupInputs(
                "workspace",
                context.workspace.name,
                "workspace",
                tuple(workspace.harness_integrations),
                SecretTarget(vm=vm.env, workspace=workspace.env),
                workspace.artifacts,
            )
        )
    if context.session is not None:
        session = session_template(db, registry, context.session.name, context.session.template)
        owners.append(
            SetupInputs(
                "session",
                context.session.name,
                "session",
                (CapabilityBlock.of(session.harness_integration, **session.harness_integration_config),),
                SecretTarget(vm=vm.env, admin=admin_env, agent=user_env, workspace=workspace_env, session=session.env),
                session.artifacts,
            )
        )
    return tuple(owners)


def inspect_artifacts(
    db: Database,
    registry: Registry,
    *,
    vm_name: str | None = None,
    admin: bool = False,
    agent_name: str | None = None,
    workspace_name: str | None = None,
    session_name: str | None = None,
    integration_name: str | None = None,
) -> ArtifactInspection:
    """Project declarations and recorded application without acquiring or applying anything."""
    from agentworks.artifacts.routing import deferred_inputs, inspect_owner_artifacts, session_inherited_inputs

    with db.snapshot():
        context = resolve_context(
            db,
            vm_name=vm_name,
            admin=admin,
            agent_name=agent_name,
            workspace_name=workspace_name,
            session_name=session_name,
        )
        owners = _owner_inputs(db, registry, context)
        names: dict[str, None] = {}
        diagnostics: dict[ArtifactComponent, tuple[str, ...]] = {}
        for owner in owners:
            names.update((block.name, None) for block in owner.activations)
            try:
                names.update(
                    (record.integration, None)
                    for record in read_native_setup(db, owner.kind, owner.name).records
                    if record.component == owner.component
                )
            except StateError:
                diagnostics[owner.component] = ("Native setup evidence is malformed or unavailable.",)
        if integration_name is not None:
            try:
                registry.lookup("harness-integration", integration_name)
            except KeyError:
                raise NotFoundError(
                    "harness integration not found", entity_kind="harness-integration", entity_name=integration_name
                ) from None
            names = {integration_name: None}
        integrations: dict[ArtifactComponent, list[IntegrationMetadata]] = {owner.component: [] for owner in owners}
        for name in names:
            views: dict[ArtifactComponent, ArtifactOwnerView] = {}
            for owner in owners:
                inherited: dict[ArtifactOwner, ArtifactGroup] | None = {}
                if owner.component in ("admin", "agent", "workspace"):
                    inherited = deferred_inputs(views["vm"], "workspace" if owner.component == "workspace" else "user")
                elif owner.component == "session":
                    inherited = session_inherited_inputs(
                        views["vm"], views["admin" if context.admin else "agent"], views["workspace"]
                    )
                view = inspect_owner_artifacts(db, registry, owner, name, inherited=inherited)
                views[owner.component] = view
                integrations[owner.component].append(_integration_metadata(owner, name, view))
        projected = []
        for owner in owners:
            view = inspect_owner_artifacts(db, registry, owner)
            projected.append(
                OwnerMetadata(
                    owner.component,
                    owner.kind,
                    owner.name,
                    owner.facet,
                    tuple(owner.artifacts.bundles),
                    view.capture_status,
                    _artifact_metadata(registry, owner.artifacts, view),
                    tuple(integrations[owner.component]),
                    diagnostics.get(owner.component, ()) + ((view.reason,) if view.reason else ()),
                )
            )
        return ArtifactInspection(tuple(projected))


def _artifact_metadata(
    registry: Registry, config: ArtifactsConfig, view: ArtifactOwnerView
) -> tuple[ArtifactMetadata, ...]:
    entries: dict[tuple[str, str], tuple[str, ArtifactSpec]] = {}
    for bundle in config.bundles:
        try:
            declaration = resolve_bundle(registry, bundle).value
        except KeyError:
            continue  # Missing bundle references remain visible in the owner declaration.
        for kind in ArtifactType:
            entries.update(
                ((kind.value, name), (bundle, spec)) for name, spec in getattr(declaration, kind.value + "s").items()
            )
    rows = []
    captured = () if view.captured is None else view.captured.inputs.items()
    seen = set()
    for item in captured:
        key = (item.content.type.value, item.content.name)
        selected = entries.get(key)
        declared = selected is not None and selected[0] == item.origin.bundle
        if declared:
            seen.add(key)
        rows.append(
            ArtifactMetadata(
                item.origin.bundle,
                item.content.name,
                item.content.type.value,
                declared,
                item.identity,
                item.origin_identity,
                item.content.name,
                item.provenance.source,
                item.provenance.requested_ref or None,
                item.provenance.commit or None,
                item.provenance.selected_path or None,
                item.replacements,
            )
        )
    rows.extend(
        ArtifactMetadata(bundle, name, kind, True)
        for (kind, name), (bundle, _) in entries.items()
        if (kind, name) not in seen
    )
    return tuple(rows)


def _integration_metadata(owner: SetupInputs, name: str, view: ArtifactOwnerView) -> IntegrationMetadata:
    from agentworks.artifacts.routing import deferred_inputs, inactive_destination

    destination = (
        inactive_destination(owner.facet) if view.status == "inactive" and owner.component != "session" else None
    )
    passthrough = deferred_inputs(view, destination) if destination is not None else {}
    record = view.record
    deferred = (
        ()
        if record is None
        else tuple(DeferralMetadata(item.input_id, item.destination, item.reason) for item in record.deferred)
    )
    deferred_ids = {item.input_id for item in deferred}
    handled = (
        ()
        if record is None or not record.complete
        else tuple(input_id for input_id in record.artifact_inputs or () if input_id not in deferred_ids)
    )
    placements = (
        ()
        if record is None
        else tuple(PlacementMetadata(item.path, item.native_identity, item.origins) for item in record.artifact_files)
    )
    return IntegrationMetadata(
        name,
        any(block.name == name for block in owner.activations),
        view.status,
        view.reason,
        handled,
        deferred,
        placements,
        tuple(item.identity for group in (passthrough or {}).values() for item in group.items()),
        destination if passthrough else None,
    )


def render_artifacts(inspection: ArtifactInspection) -> None:
    """Render only the safe metadata projection, never capture bodies or config."""
    for owner in inspection.owners:
        output.info(f"{owner.scope}/{owner.resource_name} (facet: {owner.facet}; capture: {owner.capture_status})")
        output.info(f"  Bundles: {', '.join(owner.bundles) or '(none declared)'}")
        for diagnostic in owner.diagnostics:
            output.info(f"  {diagnostic}")
        for artifact in owner.artifacts:
            content = artifact.input_id[:12] if artifact.input_id is not None else "contents unknown"
            revision = artifact.revision or ("not applicable" if artifact.input_id is not None else "unknown")
            current = "declared" if artifact.declared else "removed declaration"
            output.info(
                f"  {artifact.type} {artifact.bundle}/{artifact.entry}: {content}; {current}; revision {revision}"
            )
            for previous in artifact.replacements:
                output.info(
                    f"    Replaced bundle {previous.origin.bundle}: {previous.digest[:12]}; "
                    f"source {previous.provenance.source}"
                )
            if artifact.source is not None:
                output.info(f"    Source: {artifact.source}; native name: {artifact.native_name}")
                if artifact.selected_path:
                    output.info(
                        f"    Selected path: {artifact.selected_path}; "
                        f"requested ref: {artifact.requested_ref or 'default'}"
                    )
        for integration in owner.integrations:
            activation = "activated" if integration.activated else "inactive"
            details = [activation]
            if integration.status != activation:
                details.append(integration.status)
            if integration.reason:
                details.append(integration.reason)
            output.info(f"  {integration.name}: {'; '.join(details)}")
            for input_id in integration.passthrough_inputs:
                output.info(f"    Core passthrough: {input_id[:12]} to {integration.passthrough_destination}")
            for input_id in integration.recorded_handled:
                output.info(f"    Recorded handled: {input_id[:12]}")
            for item in integration.recorded_deferred:
                output.info(f"    Recorded deferred: {item.input_id[:12]} to {item.destination}: {item.reason}")
            for placement in integration.placements:
                output.info(f"    Recorded placement: {placement.path} ({placement.native_identity or 'file'})")
    output.info("Recorded application does not verify current native files or model context.")


def inspection_data(inspection: ArtifactInspection) -> JsonObject:
    """Encode only explicit metadata fields into the standard machine-output envelope."""
    return {
        "owners": [
            {
                "scope": owner.scope,
                "resource_kind": owner.resource_kind,
                "resource_name": owner.resource_name,
                "facet": owner.facet,
                "bundles": list(owner.bundles),
                "capture_status": owner.capture_status,
                "diagnostics": list(owner.diagnostics),
                "artifacts": [
                    {
                        "bundle": item.bundle,
                        "entry": item.entry,
                        "type": item.type,
                        "declared": item.declared,
                        "input_id": item.input_id,
                        "origin_id": item.origin_id,
                        "native_name": item.native_name,
                        "source": item.source,
                        "requested_ref": item.requested_ref,
                        "revision": item.revision,
                        "selected_path": item.selected_path,
                        "replacements": [
                            {
                                "bundle": previous.origin.bundle,
                                "producer": previous.origin.producer,
                                "source": previous.provenance.source,
                                "requested_ref": previous.provenance.requested_ref,
                                "selected_path": previous.provenance.selected_path,
                                "revision": previous.provenance.commit,
                                "digest": previous.digest,
                            }
                            for previous in item.replacements
                        ],
                    }
                    for item in owner.artifacts
                ],
                "integrations": [
                    {
                        "name": item.name,
                        "activated": item.activated,
                        "status": item.status,
                        "reason": item.reason,
                        "recorded_handled": list(item.recorded_handled),
                        "passthrough_inputs": list(item.passthrough_inputs),
                        "passthrough_destination": item.passthrough_destination,
                        "recorded_deferred": [
                            {"input_id": value.input_id, "destination": value.destination, "reason": value.reason}
                            for value in item.recorded_deferred
                        ],
                        "placements": [
                            {
                                "path": value.path,
                                "native_identity": value.native_identity,
                                "origins": list(value.origins),
                            }
                            for value in item.placements
                        ],
                    }
                    for item in owner.integrations
                ],
            }
            for owner in inspection.owners
        ]
    }
