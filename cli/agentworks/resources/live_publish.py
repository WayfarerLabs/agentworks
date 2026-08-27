"""Publish typed database-backed live resources into a mutable Registry."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from agentworks.resources.live import LiveResource
from agentworks.resources.reference import ResourceReference

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.agents.template import AgentTemplate
    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.db import AgentRow, Database, SessionRow, VMRow, WorkspaceRow
    from agentworks.resources.inheritance import LayeredResolution
    from agentworks.resources.registry import Registry
    from agentworks.sessions.template import SessionTemplate
    from agentworks.sessions.templates import ResolvedSessionTemplate
    from agentworks.vms.admin import AdminConfig
    from agentworks.vms.templates import ResolvedVMTemplate
    from agentworks.workspaces.template import WorkspaceTemplate
    from agentworks.workspaces.templates import ResolvedTemplate as ResolvedWorkspaceTemplate


def _selected(
    source: tuple[str, str],
    kind: str,
    name: str,
    usage: str,
) -> ResourceReference:
    return ResourceReference(name=name, kind=kind, usage=usage, source=source)


def _targets(references: tuple[ResourceReference, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(dict.fromkeys((ref.kind, ref.name) for ref in references))


def _is_published(registry: Registry, kind: str, name: str) -> bool:
    try:
        registry.lookup(kind, name)
    except KeyError:
        return False
    return True


def _published_name(registry: Registry, kind: str, name: str) -> str | None:
    """Retain a durable selector only while its target is published."""
    return name if _is_published(registry, kind, name) else None


def _published_live(registry: Registry, kind: str, name: str | None) -> LiveResource | None:
    if name is None:
        return None
    try:
        resource = registry.lookup(kind, name)
    except KeyError:
        return None
    return resource if isinstance(resource, LiveResource) else None


def _outbound_target(resource: LiveResource | None, kind: str) -> str | None:
    if resource is None:
        return None
    return next((ref.name for ref in resource.outbound if ref.kind == kind), None)


def project_agent_live_resource(
    *,
    name: str,
    vm_name: str | None,
    template_name: str | None,
    layered: LayeredResolution[ResolvedAgentTemplate] | None,
) -> LiveResource:
    """Project one agent from its identity and typed effective declaration."""
    from agentworks.agents.template import effective_references

    source = ("agent", name)
    desired = (
        *(
            ()
            if template_name is None
            else (_selected(source, "agent-template", template_name, "the selected agent template"),)
        ),
        *(() if layered is None else effective_references(layered.value, source, layered.provenance)),
    )
    owner = () if vm_name is None else (_selected(source, "vm", vm_name, "the owning VM"),)
    return LiveResource(*source, (*owner, *desired), environment_targets=_targets(desired))


def project_workspace_live_resource(
    *,
    name: str,
    vm_name: str | None,
    template_name: str | None,
    layered: LayeredResolution[ResolvedWorkspaceTemplate] | None,
) -> LiveResource:
    """Project one workspace from its identity and typed effective declaration."""
    from agentworks.workspaces.template import effective_references

    source = ("workspace", name)
    desired = (
        *(
            ()
            if template_name is None
            else (_selected(source, "workspace-template", template_name, "the selected workspace template"),)
        ),
        *(() if layered is None else effective_references(layered.value, source, layered.provenance)),
    )
    owner = () if vm_name is None else (_selected(source, "vm", vm_name, "the owning VM"),)
    return LiveResource(*source, (*owner, *desired), environment_targets=_targets(desired))


def project_session_live_resource(
    *,
    name: str,
    workspace_name: str | None,
    agent_name: str | None,
    template_name: str | None,
    layered: LayeredResolution[ResolvedSessionTemplate] | None,
) -> LiveResource:
    """Project one session from its identity and typed effective declaration."""
    from agentworks.sessions.template import effective_references, validate_effective_harness

    source = ("session", name)
    owner_refs = (
        *(() if workspace_name is None else (_selected(source, "workspace", workspace_name, "the owning workspace"),)),
        *(() if agent_name is None else (_selected(source, "agent", agent_name, "the session agent"),)),
    )
    if layered is not None:
        validate_effective_harness(layered.value, source)
    desired = (
        *(
            ()
            if template_name is None
            else (_selected(source, "session-template", template_name, "the selected session template"),)
        ),
        *(() if layered is None else effective_references(layered.value, source, layered.provenance)),
    )
    return LiveResource(*source, (*owner_refs, *desired), environment_targets=_targets(desired))


def project_vm_live_resource(
    *,
    name: str,
    site: str | None,
    vm_template_name: str | None,
    admin_template_name: str | None,
    layered_vm: LayeredResolution[ResolvedVMTemplate] | None,
    layered_admin: LayeredResolution[AdminConfig] | None,
) -> LiveResource:
    """Project one VM from its identity and paired effective declarations."""
    from agentworks.vms.admin import effective_references as admin_effective_references
    from agentworks.vms.template import effective_references as vm_effective_references

    source = ("vm", name)
    vm_desired = (
        *(
            ()
            if vm_template_name is None
            else (_selected(source, "vm-template", vm_template_name, "the selected VM template"),)
        ),
        *(() if layered_vm is None else vm_effective_references(layered_vm.value, source, layered_vm.provenance)),
    )
    admin_desired = (
        *(
            ()
            if admin_template_name is None
            else (_selected(source, "admin-template", admin_template_name, "the selected admin template"),)
        ),
        *(
            ()
            if layered_admin is None
            else admin_effective_references(
                layered_admin.value,
                source,
                layered_admin.provenance,
            )
        ),
    )
    placement = () if site is None else (_selected(source, "vm-site", site, "the VM site"),)
    return LiveResource(
        *source,
        (*placement, *vm_desired, *admin_desired),
        environment_targets=_targets(vm_desired),
        admin_environment_targets=_targets(admin_desired),
    )


def project_console_live_resource(
    *,
    name: str,
    vm_name: str | None,
    session_names: tuple[str, ...],
) -> LiveResource:
    """Project one named console and its intrinsic live relationships."""
    source = ("console", name)
    refs = (
        *(() if vm_name is None else (_selected(source, "vm", vm_name, "the console VM"),)),
        _selected(source, "named-console-template", "default", "the named console template"),
        *(_selected(source, "session", session_name, "a console session") for session_name in session_names),
    )
    return LiveResource(*source, refs)


def agent_live_resource(db: Database, registry: Registry, row: AgentRow) -> LiveResource:
    """Project one persisted agent's fully resolved desired references."""
    from agentworks.agents.templates import resolve_template_with_provenance
    from agentworks.instance_specs import get_instance_overlay

    selected = row.template or "default"
    overlay = get_instance_overlay(db, "agent", row.name)
    if not _is_published(registry, "agent-template", selected):
        return project_agent_live_resource(
            name=row.name,
            vm_name=_published_name(registry, "vm", row.vm_name),
            template_name=None,
            layered=None,
        )
    layered = resolve_template_with_provenance(
        registry,
        selected,
        overlay=None if overlay is None else cast("AgentTemplate", overlay.declaration),
        instance_name=row.name,
    )
    return project_agent_live_resource(
        name=row.name,
        vm_name=_published_name(registry, "vm", row.vm_name),
        template_name=selected,
        layered=layered,
    )


def workspace_live_resource(db: Database, registry: Registry, row: WorkspaceRow) -> LiveResource:
    """Project one persisted workspace's fully resolved desired references."""
    from agentworks.instance_specs import get_instance_overlay
    from agentworks.workspaces.templates import resolve_template_with_provenance

    selected = row.template or "default"
    overlay = get_instance_overlay(db, "workspace", row.name)
    if not _is_published(registry, "workspace-template", selected):
        return project_workspace_live_resource(
            name=row.name,
            vm_name=_published_name(registry, "vm", row.vm_name),
            template_name=None,
            layered=None,
        )
    layered = resolve_template_with_provenance(
        registry,
        selected,
        overlay=None if overlay is None else cast("WorkspaceTemplate", overlay.declaration),
        instance_name=row.name,
    )
    return project_workspace_live_resource(
        name=row.name,
        vm_name=_published_name(registry, "vm", row.vm_name),
        template_name=selected,
        layered=layered,
    )


def session_live_resource(db: Database, registry: Registry, row: SessionRow) -> LiveResource:
    """Project one persisted session's fully resolved desired references."""
    from agentworks.instance_specs import get_instance_overlay
    from agentworks.sessions.templates import resolve_template_with_provenance

    overlay = get_instance_overlay(db, "session", row.name)
    if not _is_published(registry, "session-template", row.template):
        projected = project_session_live_resource(
            name=row.name,
            workspace_name=_published_name(registry, "workspace", row.workspace_name),
            agent_name=(None if row.agent_name is None else _published_name(registry, "agent", row.agent_name)),
            template_name=None,
            layered=None,
        )
    else:
        layered = resolve_template_with_provenance(
            registry,
            row.template,
            overlay=None if overlay is None else cast("SessionTemplate", overlay.declaration),
            instance_name=row.name,
        )
        projected = project_session_live_resource(
            name=row.name,
            workspace_name=_published_name(registry, "workspace", row.workspace_name),
            agent_name=(None if row.agent_name is None else _published_name(registry, "agent", row.agent_name)),
            template_name=row.template,
            layered=layered,
        )

    targets = list(projected.environment_targets)
    workspace = _published_live(registry, "workspace", row.workspace_name)
    if workspace is not None:
        targets.extend(workspace.environment_targets)
    vm = _published_live(registry, "vm", _outbound_target(workspace, "vm"))
    if vm is not None:
        targets.extend(vm.environment_targets)

    from agentworks.db import SessionMode

    if row.mode == SessionMode.ADMIN.value:
        if vm is not None:
            targets.extend(vm.admin_environment_targets)
    elif row.mode == SessionMode.AGENT.value and row.agent_name is not None:
        agent = _published_live(registry, "agent", row.agent_name)
        if agent is not None:
            targets.extend(agent.environment_targets)

    return replace(projected, environment_targets=tuple(dict.fromkeys(targets)))


def vm_live_resource(db: Database, registry: Registry, row: VMRow) -> LiveResource:
    """Project one persisted VM's paired VM and admin desired references."""
    from agentworks.instance_specs import get_vm_instance_overlays
    from agentworks.vms.admin_templates import resolve_template_with_provenance as resolve_admin
    from agentworks.vms.templates import resolve_template_with_provenance as resolve_vm

    selected_vm = row.template or "default"
    selected_admin = row.admin_template or "default"
    overlays = get_vm_instance_overlays(db, row.name)
    vm_template_name = _published_name(registry, "vm-template", selected_vm)
    admin_template_name = _published_name(registry, "admin-template", selected_admin)
    layered_vm = (
        None
        if vm_template_name is None
        else resolve_vm(
            registry,
            selected_vm,
            overlay=None if overlays is None or overlays.vm is None else overlays.vm.declaration,
            instance_name=row.name,
        )
    )
    layered_admin = (
        None
        if admin_template_name is None
        else resolve_admin(
            registry,
            selected_admin,
            overlay=None if overlays is None else overlays.admin,
            instance_name=row.name,
        )
    )
    return project_vm_live_resource(
        name=row.name,
        site=_published_name(registry, "vm-site", row.site),
        vm_template_name=vm_template_name,
        admin_template_name=admin_template_name,
        layered_vm=layered_vm,
        layered_admin=layered_admin,
    )


def _publish_database_rows(registry: Registry, db: Database) -> None:
    for vm in db.list_vms():
        if registry.has_live("vm", vm.name):
            continue
        registry.add_live(vm_live_resource(db, registry, vm))
    for workspace in db.list_workspaces():
        if registry.has_live("workspace", workspace.name):
            continue
        registry.add_live(workspace_live_resource(db, registry, workspace))
    for agent in db.list_agents():
        if registry.has_live("agent", agent.name):
            continue
        registry.add_live(agent_live_resource(db, registry, agent))
    for session in db.list_sessions():
        if registry.has_live("session", session.name):
            continue
        registry.add_live(session_live_resource(db, registry, session))
    for console in db.list_consoles():
        if registry.has_live("console", console.name):
            continue
        registry.add_live(
            project_console_live_resource(
                name=console.name,
                vm_name=_published_name(registry, "vm", console.vm_name),
                session_names=tuple(
                    member.session_name
                    for member in db.list_console_sessions(console.name)
                    if _is_published(registry, "session", member.session_name)
                ),
            )
        )


def publish_open_database_live_resources(registry: Registry, db: Database) -> None:
    """Publish from a caller-owned writable or read-only database snapshot."""
    with db.snapshot():
        _publish_database_rows(registry, db)


def publish_database_live_resources(registry: Registry, database_path: Path) -> None:
    """Publish every database-backed resource from one read snapshot.

    An absent database is a normal empty publisher. Existing databases are
    opened read-only and must have the current schema; malformed, outdated,
    and unsupported desired state remains a typed failure rather than an
    incomplete graph.
    """
    try:
        database_path.stat()
    except FileNotFoundError:
        return
    except OSError as error:
        from agentworks.errors import StateError

        raise StateError("state database inspection failed", entity_kind="database") from error

    from agentworks.db import Database

    db = Database(database_path, read_only=True)
    try:
        with db.snapshot():
            _publish_database_rows(registry, db)
    finally:
        db.close()
