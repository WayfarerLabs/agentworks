"""VM list and describe facts, machine projections, and human rendering."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast

from agentworks import output
from agentworks.db import SYSTEM_SLUG_KEY, VMStatus
from agentworks.db.projections import (
    project_session_mode,
    project_vm_initialization_status,
    project_vm_provisioning_status,
)
from agentworks.debian import classify_release
from agentworks.errors import (
    AgentworksError,
    UserAbort,
)
from agentworks.naming import MAX_VM_NAME_LENGTH

from ._helpers import _require_vm, _vm_scope
from .boundary import _platform_ops_ctx, _warn_legacy_release

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, DesiredOverlayRecord, InstanceStateInspection, VMRow
    from agentworks.instance_description import InstanceStateDescription
    from agentworks.machine_output import JsonObject
    from agentworks.resources.registry import Registry
    from agentworks.secrets.policy import TtyInteractionPolicy
    from agentworks.vms.applied_state import SSHAppliedState

# NAME-column truncation cap for ``vm list``, derived from the VM-name cap so
# the two cannot drift: a valid name (<= MAX_VM_NAME_LENGTH) never truncates,
# and the column stays aligned even against an over-cap legacy row.
_NAME_CELL_WIDTH = MAX_VM_NAME_LENGTH


@dataclass(frozen=True)
class VMListRow:
    name: str
    site: str
    template: str | None
    provisioning_status: str
    initialization_status: str
    workspace_count: int
    agent_count: int
    session_count: int
    tailscale_host: str | None
    created_at: str
    debian_release: str | None = None
    debian_release_observed_at: str | None = None
    debian_support: str | None = None


@dataclass(frozen=True)
class VMListing:
    vms: tuple[VMListRow, ...]


class VMInspectionIssueSource(StrEnum):
    """Closed failure stages that JSON v1 may disclose for VM inspection."""

    SITE_LOOKUP = "site_lookup"
    PREFLIGHT = "preflight"
    SECRET_RESOLUTION = "secret_resolution"
    PLATFORM_STATUS = "platform_status"


# The only outcome JSON v1 discloses for a VM inspection issue: the stage named
# by the issue's source could not report. Every issue this module raises is that
# outcome, so the JSON code is a constant rather than a per-issue fact.
_VM_ISSUE_CODE_UNAVAILABLE = "unavailable"
_HARDWARE_REQUEST_EVIDENCE_KEY = "hardware-request"


@dataclass(frozen=True)
class VMIssue:
    source: VMInspectionIssueSource


@dataclass(frozen=True)
class VMDiagnostic:
    issue: VMIssue
    error: AgentworksError


@dataclass(frozen=True)
class VMDetailAgent:
    name: str
    linux_user: str
    grant_all: bool
    grant_count: int


@dataclass(frozen=True)
class VMDetailSession:
    name: str
    template: str
    mode: str
    agent_name: str | None


@dataclass(frozen=True)
class VMDetailWorkspace:
    name: str
    path: str
    sessions: tuple[VMDetailSession, ...]


@dataclass(frozen=True)
class VMDetailEvent:
    created_at: str
    event: str
    detail: str | None


class VMEventName(StrEnum):
    """Closed JSON v1 vocabulary for persisted VM event names."""

    PROVISIONING_STARTED = "provisioning_started"
    PROVISIONING_COMPLETE = "provisioning_complete"
    PROVISIONING_FAILED = "provisioning_failed"
    INIT_STARTED = "init_started"
    INIT_COMPLETE = "init_complete"
    INIT_PARTIAL = "init_partial"
    INIT_FAILED = "init_failed"
    BACKUP_STARTED = "backup_started"
    BACKUP_COMPLETED = "backup_completed"
    BACKUP_FAILED = "backup_failed"
    DEBIAN_UPGRADE_STARTED = "debian_upgrade_started"
    DEBIAN_UPGRADE_COMPLETE = "debian_upgrade_complete"
    DEBIAN_UPGRADE_ADOPTED = "debian_upgrade_adopted"
    DEBIAN_UPGRADE_REPAIR_REQUIRED = "debian_upgrade_repair_required"
    REKEY = "rekey"
    UNKNOWN = "unknown"


def _project_vm_event_name(raw_name: str) -> str:
    """Map historical or future raw event names to the stable sentinel."""
    try:
        return VMEventName(raw_name).value
    except ValueError:
        return VMEventName.UNKNOWN.value


@dataclass(frozen=True)
class VMDetailFacts:
    """Immutable safe scalar snapshot of the persisted VM row."""

    name: str
    site: str
    template: str | None
    admin_template: str | None
    provisioning_status: str
    initialization_status: str
    tailscale_host: str | None
    cpus: int | None
    memory_gib: int | None
    disk_gib: int | None
    swap_gib: int | None
    admin_username: str
    hostname: str
    created_at: str
    last_seen_at: str | None
    operator_stopped: bool
    debian_release: str | None = None
    debian_release_observed_at: str | None = None
    debian_support: str | None = None

    @classmethod
    def from_row(cls, vm: VMRow) -> VMDetailFacts:
        """Copy only contract-safe scalar values from a mutable database row."""
        return cls(
            name=vm.name,
            site=vm.site,
            template=vm.template,
            admin_template=vm.admin_template,
            provisioning_status=project_vm_provisioning_status(vm.provisioning_status),
            initialization_status=project_vm_initialization_status(vm.init_status),
            tailscale_host=vm.tailscale_host,
            cpus=vm.cpus,
            memory_gib=vm.memory_gib,
            disk_gib=vm.disk_gib,
            swap_gib=vm.swap_gib,
            admin_username=vm.admin_username,
            hostname=vm.hostname,
            created_at=vm.created_at,
            last_seen_at=vm.last_seen_at,
            operator_stopped=vm.operator_stopped,
            debian_release=vm.debian_release.value if vm.debian_release is not None else None,
            debian_release_observed_at=vm.debian_release_observed_at,
            debian_support=classify_release(vm.debian_release).value if vm.debian_release is not None else None,
        )


@dataclass(frozen=True)
class VMLiveResources:
    """Immutable safe scalar snapshot of a successful live resource read."""

    cpus: str
    load_average: str
    memory_total: str
    memory_used: str
    memory_percent: str
    swap_total: str
    swap_used: str
    swap_percent: str
    disk_total: str
    disk_used: str
    disk_percent: str

    @classmethod
    def from_mapping(cls, resources: dict[str, str]) -> VMLiveResources:
        return cls(
            cpus=resources["cpus"],
            load_average=resources["load_avg"],
            memory_total=resources["mem_total"],
            memory_used=resources["mem_used"],
            memory_percent=resources["mem_pct"],
            swap_total=resources["swap_total"],
            swap_used=resources["swap_used"],
            swap_percent=resources["swap_pct"],
            disk_total=resources["disk_total"],
            disk_used=resources["disk_used"],
            disk_percent=resources["disk_pct"],
        )


@dataclass(frozen=True)
class VMDescription:
    vm: VMDetailFacts
    platform: str | None
    backend: str | None
    observed_status: str | None
    status_disposition: str | None
    system_slug: str | None
    system_slug_state: str
    live_resources: VMLiveResources | None
    agents: tuple[VMDetailAgent, ...]
    workspaces: tuple[VMDetailWorkspace, ...]
    events: tuple[VMDetailEvent, ...]
    issues: tuple[VMIssue, ...]
    diagnostics: tuple[VMDiagnostic, ...]
    instance_state: InstanceStateDescription


def vm_listing_data(listing: VMListing) -> JsonObject:
    """Project VM list facts into the closed JSON v1 shape."""
    return {
        "vms": [
            {
                "name": vm.name,
                "site": vm.site,
                "template": vm.template,
                "provisioning_status": project_vm_provisioning_status(vm.provisioning_status),
                "initialization_status": project_vm_initialization_status(vm.initialization_status),
                "workspace_count": vm.workspace_count,
                "agent_count": vm.agent_count,
                "session_count": vm.session_count,
                "tailscale_host": vm.tailscale_host,
                "created_at": vm.created_at,
                "debian_release": vm.debian_release,
                "debian_release_observed_at": vm.debian_release_observed_at,
            }
            for vm in listing.vms
        ],
    }


def vm_description_data(description: VMDescription) -> JsonObject:
    """Project VM detail facts into the closed JSON v1 shape."""
    vm = description.vm
    live_resources = description.live_resources
    data: JsonObject = {
        "vm": {
            "name": vm.name,
            "created_at": vm.created_at,
            "site": vm.site,
            "platform": description.platform,
            "backend": description.backend,
            "observed_status": description.observed_status,
            "status_disposition": description.status_disposition,
            "operator_stopped": vm.operator_stopped,
            "hostname": vm.hostname,
            "system_slug": description.system_slug,
            "system_slug_state": description.system_slug_state,
            "template": vm.template,
            "admin_template": vm.admin_template,
            "admin_username": vm.admin_username,
            "provisioning_status": project_vm_provisioning_status(vm.provisioning_status),
            "initialization_status": project_vm_initialization_status(vm.initialization_status),
            "tailscale_host": vm.tailscale_host,
            "last_seen_at": vm.last_seen_at,
            "debian_release": vm.debian_release,
            "debian_release_observed_at": vm.debian_release_observed_at,
            "provisioned_resources": {
                "cpus": vm.cpus,
                "memory_gib": vm.memory_gib,
                "disk_gib": vm.disk_gib,
                "swap_gib": vm.swap_gib,
            },
            "live_resources": None
            if live_resources is None
            else {
                "cpus": live_resources.cpus,
                "load_average": live_resources.load_average,
                "memory_total": live_resources.memory_total,
                "memory_used": live_resources.memory_used,
                "memory_percent": live_resources.memory_percent,
                "swap_total": live_resources.swap_total,
                "swap_used": live_resources.swap_used,
                "swap_percent": live_resources.swap_percent,
                "disk_total": live_resources.disk_total,
                "disk_used": live_resources.disk_used,
                "disk_percent": live_resources.disk_percent,
            },
            "agents": [
                {
                    "name": agent.name,
                    "linux_user": agent.linux_user,
                    "grant_all": agent.grant_all,
                    "grant_count": agent.grant_count,
                }
                for agent in description.agents
            ],
            "workspaces": [
                {
                    "name": workspace.name,
                    "path": workspace.path,
                    "sessions": [
                        {
                            "name": session.name,
                            "template": session.template,
                            "mode": project_session_mode(session.mode),
                            "agent_name": session.agent_name,
                        }
                        for session in workspace.sessions
                    ],
                }
                for workspace in description.workspaces
            ],
            # Event details are historical free text, including messages written
            # by older failure paths. They have no closed safe contract, so JSON
            # must fail closed rather than expose a command or secret-bearing
            # diagnostic. The human renderer retains the legacy detail display.
            "events": [
                {
                    "created_at": event.created_at,
                    "event": _project_vm_event_name(event.event),
                    "detail": None,
                }
                for event in description.events
            ],
        },
        "issues": [_project_vm_issue(issue) for issue in description.issues],
    }
    from agentworks.instance_description import instance_state_data

    vm_data = cast("JsonObject", data["vm"])
    vm_data["instance_state"] = instance_state_data(description.instance_state)
    return data


def _project_vm_issue(issue: VMIssue) -> JsonObject:
    """Project only the closed issue vocabulary, failing shut on bad facts."""
    if not isinstance(issue.source, VMInspectionIssueSource):
        raise AssertionError("VM issues require a closed source value")
    return {"source": issue.source.value, "code": _VM_ISSUE_CODE_UNAVAILABLE}


# NOTE on ``_ensure_tailscale`` (start_vm), ``_tailscale_logout``
# (delete_vm), and ``_query_live_resources`` (describe_vm): all three are
# defined elsewhere (``tailscale.py``, ``_helpers.py``), and tests
# monkeypatch them as attributes of the PACKAGE
# (``agentworks.vms.manager._ensure_tailscale`` / ``._tailscale_logout`` /
# ``._query_live_resources``). A plain ``from .tailscale import
# _ensure_tailscale`` (or ``from ._helpers import
# _query_live_resources``) here would bind a local name in THIS module's
# namespace, invisible to a monkeypatch of the package attribute. All
# three calls below go through ``import agentworks.vms.manager as _mgr``
# at call time instead.


def vm_listing(db: Database) -> VMListing:
    """Collect ordered VM list facts without presentation."""
    return VMListing(
        vms=tuple(
            VMListRow(
                name=vm.name,
                site=vm.site,
                template=vm.template,
                provisioning_status=project_vm_provisioning_status(vm.provisioning_status),
                initialization_status=project_vm_initialization_status(vm.init_status),
                workspace_count=db.count_workspaces_on_vm(vm.name),
                agent_count=db.count_agents_on_vm(vm.name),
                session_count=db.count_sessions_on_vm(vm.name),
                tailscale_host=vm.tailscale_host,
                created_at=vm.created_at,
                debian_release=vm.debian_release.value if vm.debian_release is not None else None,
                debian_release_observed_at=vm.debian_release_observed_at,
                debian_support=classify_release(vm.debian_release).value if vm.debian_release is not None else None,
            )
            for vm in db.list_vms()
        )
    )


def render_vm_listing(listing: VMListing, *, names_only: bool = False) -> None:
    """Render VM list facts with the legacy human layout.

    With ``names_only=True``, emit one VM name per line and skip the
    table render. Used by shell completion (see issue #147).
    """
    vms = listing.vms

    if names_only:
        # Names-only short-circuits BEFORE the empty check so an
        # empty db prints nothing (not the friendly "No VMs"
        # message), keeping the completion candidate set clean.
        for vm in vms:
            output.info(vm.name)
        return

    if not vms:
        output.info("No VMs registered.")
        return

    # Cap the NAME column at the VM-name cap (42) so a legacy / manually
    # inserted over-cap row cannot push the other columns out of alignment;
    # the column then sizes dynamically to the longest (truncated) name.
    names = [output.truncate(vm.name, _NAME_CELL_WIDTH) for vm in vms]
    name_w = max(len("NAME"), *(len(n) for n in names))

    header = (
        f"{'NAME':<{name_w}} {'SITE':<12} {'TEMPLATE':<12} {'PROV':<12} {'INIT':<12} "
        f"{'WS/AG/SE':<10} {'DEBIAN':<10} {'SUPPORT':<10} {'TAILSCALE':<20} {'CREATED'}"
    )
    output.info(header)
    output.info("-" * len(header))
    for vm, name in zip(vms, names, strict=True):
        counts = f"{vm.workspace_count}/{vm.agent_count}/{vm.session_count}"
        output.info(
            f"{name:<{name_w}} {vm.site:<12} {vm.template or '-':<12} "
            f"{vm.provisioning_status:<12} {vm.initialization_status:<12} "
            f"{counts:<10} {vm.debian_release or '-':<10} {vm.debian_support or '-':<10} "
            f"{vm.tailscale_host or '-':<20} {vm.created_at}"
        )


def list_vms(db: Database, *, names_only: bool = False) -> None:
    """List all VMs with their init and runtime status."""
    if names_only:
        for vm in db.list_vms():
            output.info(vm.name)
        return
    render_vm_listing(vm_listing(db))


def vm_description(
    db: Database,
    config: Config,
    name: str,
    *,
    interaction: TtyInteractionPolicy,
) -> VMDescription:
    """Collect safe VM detail facts, degrading bounded live reads to issues."""
    import agentworks.vms.manager as _mgr
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node
    from agentworks.vms.sites import lookup_site

    issues: list[VMIssue] = []
    diagnostics: list[VMDiagnostic] = []
    platform_name: str | None = None
    backend: str | None = None
    observed_status: str | None = None
    status_disposition: str | None = None
    live_resources: VMLiveResources | None = None

    with db.snapshot():
        vm = _require_vm(db, name)
        from agentworks.instance_description import load_instance_description_registry

        registry = load_instance_description_registry(db, config, "vm", name)
        inspection = db.instance_state.inspect_owner_state("vm", name)
        instance_state, applied_ssh = _vm_instance_state(registry, vm, inspection)

        stored_slug = db.get_setting(SYSTEM_SLUG_KEY)
        if stored_slug is None:
            system_slug = None
            system_slug_state = "unset"
        elif stored_slug == "":
            system_slug = None
            system_slug_state = "declined"
        else:
            system_slug = stored_slug
            system_slug_state = "set"

        agents = tuple(
            VMDetailAgent(
                name=agent.name,
                linux_user=agent.linux_user,
                grant_all=agent.grant_all,
                grant_count=db.count_agent_grants(agent.name),
            )
            for agent in db.list_agents(vm_name=name)
        )
        workspaces = tuple(
            VMDetailWorkspace(
                name=workspace.name,
                path=workspace.workspace_path,
                sessions=tuple(
                    VMDetailSession(
                        name=session.name,
                        template=session.template,
                        mode=project_session_mode(session.mode),
                        agent_name=session.agent_name,
                    )
                    for session in db.list_sessions(workspace_name=workspace.name)
                ),
            )
            for workspace in db.list_workspaces(vm_name=name)
        )
        events = tuple(
            VMDetailEvent(created_at=event.created_at, event=event.event, detail=event.detail)
            for event in db.list_vm_events(name)
        )
        vm_facts = VMDetailFacts.from_row(vm)

    _warn_legacy_release(vm)
    instance_state = _add_current_ssh_comparison(
        instance_state,
        vm.name,
        config,
        applied_ssh,
    )
    try:
        site_decl = lookup_site(vm.site, registry)
        platform_name = site_decl.platform.name
    except UserAbort:
        raise
    except AgentworksError as exc:
        issue = VMIssue(source=VMInspectionIssueSource.SITE_LOOKUP)
        issues.append(issue)
        diagnostics.append(VMDiagnostic(issue=issue, error=exc))
    else:
        resolver = Resolver(config, registry, interaction=interaction)
        try:
            vm_node = live_vm_node(db, config, registry, vm)
        except UserAbort:
            raise
        except AgentworksError as exc:
            issue = VMIssue(source=VMInspectionIssueSource.SITE_LOOKUP)
            issues.append(issue)
            diagnostics.append(VMDiagnostic(issue=issue, error=exc))
        else:
            nodes = walk(vm_node)
            for secret_name in secret_union(nodes):
                resolver.register_name(secret_name)
            scope = _vm_scope(db, vm.name)
            try:
                preflight_all(
                    nodes,
                    RunContext(config=config, operation_scope=scope),
                    registry=registry,
                    interaction=interaction,
                )
            except UserAbort:
                raise
            except AgentworksError as exc:
                issue = VMIssue(source=VMInspectionIssueSource.PREFLIGHT)
                issues.append(issue)
                diagnostics.append(VMDiagnostic(issue=issue, error=exc))
            else:
                try:
                    resolver.resolve()
                except UserAbort:
                    raise
                except AgentworksError as exc:
                    issue = VMIssue(source=VMInspectionIssueSource.SECRET_RESOLUTION)
                    issues.append(issue)
                    diagnostics.append(VMDiagnostic(issue=issue, error=exc))
                else:
                    ops_ctx = _platform_ops_ctx(config, scope, vm_node, resolver)
                    platform = vm_node.site.platform
                    try:
                        backend = platform.display_backend_name(vm)
                        observed = platform.status(vm, ops_ctx)
                        observed_status = observed.value
                        if observed in (VMStatus.STOPPED, VMStatus.DEALLOCATED):
                            status_disposition = "manual" if vm.operator_stopped else "idle"
                    except UserAbort:
                        raise
                    except AgentworksError as exc:
                        issue = VMIssue(source=VMInspectionIssueSource.PLATFORM_STATUS)
                        issues.append(issue)
                        diagnostics.append(VMDiagnostic(issue=issue, error=exc))

    # A platform-status diagnostic does not make the bounded resource helper
    # unsafe. Preserve the legacy best-effort facts unless the observation
    # definitively says the host is stopped.
    if vm.tailscale_host is not None and observed_status not in {"stopped", "deallocated"}:
        raw_live_resources = _mgr._query_live_resources(db, vm, config)
        if raw_live_resources is not None:
            live_resources = VMLiveResources.from_mapping(raw_live_resources)

    return VMDescription(
        vm=vm_facts,
        platform=platform_name,
        backend=backend,
        observed_status=observed_status,
        status_disposition=status_disposition,
        system_slug=system_slug,
        system_slug_state=system_slug_state,
        live_resources=live_resources,
        agents=agents,
        workspaces=workspaces,
        events=events,
        issues=tuple(issues),
        diagnostics=tuple(diagnostics),
        instance_state=instance_state,
    )


def _vm_instance_state(
    registry: Registry,
    vm: VMRow,
    inspection: InstanceStateInspection,
) -> tuple[InstanceStateDescription, SSHAppliedState | None]:
    """Collect same-snapshot VM declaration and lifecycle evidence."""
    from agentworks.db import AppliedStateKey
    from agentworks.errors import NotFoundError, StateError
    from agentworks.instance_description import (
        ComparisonDifference,
        DeclarationSlot,
        InstanceComparison,
        InstanceSpec,
        InstanceStateDescription,
        InstanceStateIssue,
        InstanceStateIssueCode,
        LifecycleEvidence,
        UnconsumedRecord,
        inspected_desired_record,
        inspection_metadata_facts,
        malformed_desired_record_present,
    )
    from agentworks.instance_specs import (
        UnsupportedStoredOverlayError,
        VMInstanceOverlays,
        decode_stored_vm_overlays,
    )
    from agentworks.resources.access import ResourceIdentity
    from agentworks.resources.resolved_spec import (
        ResolvedSpec,
        SpecResolution,
        UnresolvedSpec,
        project_resolved_spec,
    )
    from agentworks.vms import instance_overlay as vm_codec
    from agentworks.vms.admin_templates import resolve_template_with_provenance as resolve_admin
    from agentworks.vms.applied_state import (
        UnsupportedAppliedStateVersionError,
        UnverifiableSSHAppliedState,
        VerifiedSSHAppliedState,
        decode_hardware_provenance,
        decode_ssh_identity,
    )
    from agentworks.vms.templates import resolve_template_with_provenance as resolve_vm

    unconsumed, metadata_issues = inspection_metadata_facts(inspection)
    unconsumed_facts = list(unconsumed)
    issues = list(metadata_issues)
    record = inspected_desired_record(inspection)
    overlays: VMInstanceOverlays | None = None
    desired_unavailable: Literal["malformed", "unsupported-version"] | None = None
    if malformed_desired_record_present(inspection):
        desired_unavailable = "malformed"
    elif record is not None:
        try:
            overlays = decode_stored_vm_overlays(record)
        except UnsupportedStoredOverlayError:
            desired_unavailable = "unsupported-version"
            issues.append(InstanceStateIssue(InstanceStateIssueCode.INSTANCE_SPEC_UNSUPPORTED))
        except StateError:
            desired_unavailable = "malformed"
            issues.append(InstanceStateIssue(InstanceStateIssueCode.INSTANCE_SPEC_MALFORMED))

    vm_selection = ResourceIdentity("vm-template", "default" if vm.template is None else vm.template)
    admin_selection = ResourceIdentity(
        "admin-template",
        "default" if vm.admin_template is None else vm.admin_template,
    )

    current_vm: SpecResolution
    current_admin: SpecResolution
    if desired_unavailable is not None:
        vm_spec = admin_spec = InstanceSpec("unavailable", reason=desired_unavailable)
        current_vm = UnresolvedSpec(vm_selection, "instance-spec-unavailable")
        current_admin = UnresolvedSpec(admin_selection, "instance-spec-unavailable")
    else:
        vm_overlay = None if overlays is None else overlays.vm
        admin_overlay = None if overlays is None else overlays.admin
        if vm_overlay is not None or admin_overlay is not None:
            assert record is not None
        vm_spec = (
            InstanceSpec("absent")
            if vm_overlay is None
            else InstanceSpec(
                "present",
                recorded_at=cast("DesiredOverlayRecord", record).recorded_at,
                spec=vm_overlay.payload.value,
            )
        )
        admin_spec = (
            InstanceSpec("absent")
            if admin_overlay is None
            else InstanceSpec(
                "present",
                recorded_at=cast("DesiredOverlayRecord", record).recorded_at,
                spec=vm_codec.encode_admin_overlay(admin_overlay),
            )
        )
        try:
            current_vm = project_resolved_spec(
                resolve_vm(
                    registry,
                    vm.template,
                    overlay=None if vm_overlay is None else vm_overlay.declaration,
                    instance_name=vm.name,
                ),
                vm_selection,
            )
        except NotFoundError:
            current_vm = UnresolvedSpec(vm_selection, "missing-selection")
            issues.append(InstanceStateIssue(InstanceStateIssueCode.CURRENT_DECLARATION_UNRESOLVED, slot="vm"))
        try:
            current_admin = project_resolved_spec(
                resolve_admin(
                    registry,
                    vm.admin_template,
                    overlay=admin_overlay,
                    instance_name=vm.name,
                ),
                admin_selection,
            )
        except NotFoundError:
            current_admin = UnresolvedSpec(admin_selection, "missing-selection")
            issues.append(InstanceStateIssue(InstanceStateIssueCode.CURRENT_DECLARATION_UNRESOLVED, slot="admin"))

    applied_by_key = {item.record.key: item.record for item in inspection.applied_slices}
    malformed_applied_keys = {
        item.metadata.record_key
        for item in inspection.malformed_records
        if item.metadata.record_type == "applied-state"
    }
    lifecycle_evidence: list[LifecycleEvidence] = []
    comparisons: list[InstanceComparison] = []

    hardware = applied_by_key.get(AppliedStateKey.HARDWARE_PROVENANCE)
    if hardware is None:
        status: Literal["not-recorded", "unavailable"] = (
            "unavailable" if AppliedStateKey.HARDWARE_PROVENANCE.value in malformed_applied_keys else "not-recorded"
        )
        lifecycle_evidence.append(LifecycleEvidence(_HARDWARE_REQUEST_EVIDENCE_KEY, status))
        if status == "not-recorded":
            comparisons.append(InstanceComparison(_HARDWARE_REQUEST_EVIDENCE_KEY, "not-recorded"))
    else:
        try:
            decode_hardware_provenance(hardware)
        except UnsupportedAppliedStateVersionError:
            lifecycle_evidence.append(LifecycleEvidence(_HARDWARE_REQUEST_EVIDENCE_KEY, "unavailable"))
            unconsumed_facts.append(
                UnconsumedRecord(
                    "applied-state",
                    hardware.key.value,
                    hardware.payload.payload_version,
                    hardware.recorded_at,
                )
            )
            issues.append(
                InstanceStateIssue(
                    InstanceStateIssueCode.APPLIED_RECORD_UNSUPPORTED,
                    record_key=hardware.key.value,
                )
            )
        except StateError:
            lifecycle_evidence.append(LifecycleEvidence(_HARDWARE_REQUEST_EVIDENCE_KEY, "unavailable"))
            issues.append(
                InstanceStateIssue(
                    InstanceStateIssueCode.APPLIED_RECORD_MALFORMED,
                    record_key=hardware.key.value,
                )
            )
        else:
            values = {
                "cpus": vm.cpus,
                "memory": vm.memory_gib,
                "disk": vm.disk_gib,
                "swap": vm.swap_gib,
            }
            if not all(type(value) is int for value in values.values()):
                lifecycle_evidence.append(LifecycleEvidence(_HARDWARE_REQUEST_EVIDENCE_KEY, "unavailable"))
                issues.append(
                    InstanceStateIssue(
                        InstanceStateIssueCode.LIFECYCLE_EVIDENCE_UNAVAILABLE,
                        record_key=hardware.key.value,
                    )
                )
            else:
                recorded_values = cast("JsonObject", values)
                lifecycle_evidence.append(
                    LifecycleEvidence(
                        _HARDWARE_REQUEST_EVIDENCE_KEY,
                        "recorded",
                        recorded_at=hardware.recorded_at,
                        operation=hardware.operation,
                        value=recorded_values,
                    )
                )
                if isinstance(current_vm, ResolvedSpec):
                    differences = tuple(
                        ComparisonDifference(field, cast("int", recorded), current_vm.spec[field])
                        for field, recorded in values.items()
                        if recorded != current_vm.spec[field]
                    )
                    comparisons.append(
                        InstanceComparison(
                            _HARDWARE_REQUEST_EVIDENCE_KEY,
                            "drift" if differences else "match",
                            differences,
                        )
                    )
                else:
                    issues.append(
                        InstanceStateIssue(
                            InstanceStateIssueCode.CURRENT_DECLARATION_UNRESOLVED,
                            slot="vm",
                            record_key=hardware.key.value,
                        )
                    )

    ssh_applied: SSHAppliedState | None = None
    ssh_record = applied_by_key.get(AppliedStateKey.SSH_IDENTITY)
    if ssh_record is None:
        status = "unavailable" if AppliedStateKey.SSH_IDENTITY.value in malformed_applied_keys else "not-recorded"
        lifecycle_evidence.append(LifecycleEvidence(AppliedStateKey.SSH_IDENTITY.value, status))
        if status == "not-recorded":
            comparisons.append(InstanceComparison(AppliedStateKey.SSH_IDENTITY.value, "not-recorded"))
    else:
        try:
            ssh_applied = decode_ssh_identity(ssh_record)
        except UnsupportedAppliedStateVersionError:
            lifecycle_evidence.append(LifecycleEvidence(AppliedStateKey.SSH_IDENTITY.value, "unavailable"))
            unconsumed_facts.append(
                UnconsumedRecord(
                    "applied-state",
                    ssh_record.key.value,
                    ssh_record.payload.payload_version,
                    ssh_record.recorded_at,
                )
            )
            issues.append(
                InstanceStateIssue(
                    InstanceStateIssueCode.APPLIED_RECORD_UNSUPPORTED,
                    record_key=ssh_record.key.value,
                )
            )
        except StateError:
            lifecycle_evidence.append(LifecycleEvidence(AppliedStateKey.SSH_IDENTITY.value, "unavailable"))
            issues.append(
                InstanceStateIssue(
                    InstanceStateIssueCode.APPLIED_RECORD_MALFORMED,
                    record_key=ssh_record.key.value,
                )
            )
        else:
            if isinstance(ssh_applied, VerifiedSSHAppliedState):
                ssh_value: JsonObject = {
                    "status": "verified",
                    "private_key_ref": ssh_applied.private_key_ref,
                    "fingerprint": ssh_applied.fingerprint,
                }
            elif isinstance(ssh_applied, UnverifiableSSHAppliedState):
                ssh_value = {
                    "status": "unverifiable",
                    "private_key_ref": ssh_applied.private_key_ref,
                }
            else:
                raise AssertionError("unexpected SSH applied-state carrier")
            lifecycle_evidence.append(
                LifecycleEvidence(
                    ssh_record.key.value,
                    "recorded",
                    recorded_at=ssh_record.recorded_at,
                    operation=ssh_record.operation,
                    value=ssh_value,
                )
            )

    state = InstanceStateDescription(
        declarations=(
            DeclarationSlot("vm", vm_selection, vm_spec, current_vm),
            DeclarationSlot("admin", admin_selection, admin_spec, current_admin),
        ),
        lifecycle_evidence=tuple(lifecycle_evidence),
        comparisons=tuple(comparisons),
        unconsumed_records=tuple(unconsumed_facts),
        issues=tuple(issues),
    )
    return state, ssh_applied


def _add_current_ssh_comparison(
    state: InstanceStateDescription,
    vm_name: str,
    config: Config,
    applied: SSHAppliedState | None,
) -> InstanceStateDescription:
    """Add the host-read SSH comparison after the database snapshot closes."""
    if applied is None:
        return state
    from dataclasses import replace

    from agentworks.instance_description import (
        InstanceComparison,
        InstanceStateIssue,
        InstanceStateIssueCode,
    )
    from agentworks.ssh_identity import SSHIdentityReadError, read_private_ssh_identity
    from agentworks.vms.applied_state import (
        UnverifiableSSHAppliedState,
        compare_vm_ssh_identity_evidence,
    )

    if isinstance(applied, UnverifiableSSHAppliedState):
        return replace(
            state,
            comparisons=(
                *state.comparisons,
                InstanceComparison("ssh-identity", "unverifiable"),
            ),
        )

    try:
        current = read_private_ssh_identity(config.operator.ssh_private_key)
    except SSHIdentityReadError:
        return replace(
            state,
            issues=(
                *state.issues,
                InstanceStateIssue(
                    InstanceStateIssueCode.CURRENT_IDENTITY_UNAVAILABLE,
                    record_key="ssh-identity",
                ),
            ),
        )
    comparison = compare_vm_ssh_identity_evidence(vm_name, current, applied)
    return replace(
        state,
        comparisons=(
            *state.comparisons,
            InstanceComparison("ssh-identity", comparison.state.value),
        ),
    )


def render_vm_description(description: VMDescription) -> None:
    """Render VM detail facts with the legacy human layout."""
    from ._description_render import render_vm_description as render

    render(description)


def describe_vm(
    db: Database,
    config: Config,
    name: str,
    *,
    interaction: TtyInteractionPolicy,
) -> None:
    """Show VM details through the shared inspection fact record."""
    render_vm_description(vm_description(db, config, name, interaction=interaction))
