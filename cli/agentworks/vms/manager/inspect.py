"""VM list and describe facts, machine projections, and human rendering."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import SYSTEM_SLUG_KEY, InitStatus, ProvisioningStatus, SessionMode, VMStatus
from agentworks.db.projections import project_persisted_enum
from agentworks.errors import (
    AgentworksError,
    UserAbort,
)
from agentworks.naming import MAX_VM_NAME_LENGTH

from ._helpers import _require_vm
from .boundary import (
    InspectionBoundaryFailure,
    VMInspectionIssueSource,
    _live_vm_boundary,
)

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.machine_output import JsonObject
    from agentworks.secrets.resolve import ResolutionReporter

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


@dataclass(frozen=True)
class VMListing:
    vms: tuple[VMListRow, ...]


class VMIssueCode(StrEnum):
    """Closed JSON v1 outcome vocabulary for a VM inspection issue."""

    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class VMIssue:
    source: VMInspectionIssueSource
    code: VMIssueCode = VMIssueCode.UNAVAILABLE


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

    @classmethod
    def from_row(cls, vm: VMRow) -> VMDetailFacts:
        """Copy only contract-safe scalar values from a mutable database row."""
        return cls(
            name=vm.name,
            site=vm.site,
            template=vm.template,
            admin_template=vm.admin_template,
            provisioning_status=project_persisted_enum(vm.provisioning_status, ProvisioningStatus),
            initialization_status=project_persisted_enum(vm.init_status, InitStatus),
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


def vm_listing_data(listing: VMListing) -> JsonObject:
    """Project VM list facts into the closed JSON v1 shape."""
    return {
        "vms": [
            {
                "name": vm.name,
                "site": vm.site,
                "template": vm.template,
                "provisioning_status": project_persisted_enum(vm.provisioning_status, ProvisioningStatus),
                "initialization_status": project_persisted_enum(vm.initialization_status, InitStatus),
                "workspace_count": vm.workspace_count,
                "agent_count": vm.agent_count,
                "session_count": vm.session_count,
                "tailscale_host": vm.tailscale_host,
                "created_at": vm.created_at,
            }
            for vm in listing.vms
        ],
    }


def vm_description_data(description: VMDescription) -> JsonObject:
    """Project VM detail facts into the closed JSON v1 shape."""
    vm = description.vm
    live_resources = description.live_resources
    return {
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
            "provisioning_status": project_persisted_enum(vm.provisioning_status, ProvisioningStatus),
            "initialization_status": project_persisted_enum(vm.initialization_status, InitStatus),
            "tailscale_host": vm.tailscale_host,
            "last_seen_at": vm.last_seen_at,
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
                            "mode": project_persisted_enum(session.mode, SessionMode),
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


def _project_vm_issue(issue: VMIssue) -> JsonObject:
    """Project only the closed issue vocabulary, failing shut on bad facts."""
    if not isinstance(issue.source, VMInspectionIssueSource) or not isinstance(issue.code, VMIssueCode):
        raise AssertionError("VM issues require closed source and code values")
    return {"source": issue.source.value, "code": issue.code.value}


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
                provisioning_status=project_persisted_enum(vm.provisioning_status, ProvisioningStatus),
                initialization_status=project_persisted_enum(vm.init_status, InitStatus),
                workspace_count=db.count_workspaces_on_vm(vm.name),
                agent_count=db.count_agents_on_vm(vm.name),
                session_count=db.count_sessions_on_vm(vm.name),
                tailscale_host=vm.tailscale_host,
                created_at=vm.created_at,
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
        f"{'WS/AG/SE':<10} {'TAILSCALE':<20} {'CREATED'}"
    )
    output.info(header)
    output.info("-" * len(header))
    for vm, name in zip(vms, names, strict=True):
        counts = f"{vm.workspace_count}/{vm.agent_count}/{vm.session_count}"
        output.info(
            f"{name:<{name_w}} {vm.site:<12} {vm.template or '-':<12} "
            f"{vm.provisioning_status:<12} {vm.initialization_status:<12} "
            f"{counts:<10} {vm.tailscale_host or '-':<20} {vm.created_at}"
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
    reporter: ResolutionReporter | None = None,
) -> VMDescription:
    """Collect safe VM detail facts, degrading bounded live reads to issues."""
    import agentworks.vms.manager as _mgr
    from agentworks.bootstrap import load_request_registry
    from agentworks.vms.sites import lookup_site

    vm = _require_vm(db, name)
    issues: list[VMIssue] = []
    diagnostics: list[VMDiagnostic] = []
    platform_name: str | None = None
    backend: str | None = None
    observed_status: str | None = None
    status_disposition: str | None = None
    live_resources: VMLiveResources | None = None

    registry = load_request_registry(config)
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
        try:
            vm_node, ops_ctx = _live_vm_boundary(
                db,
                config,
                vm,
                registry=registry,
                reporter=reporter,
                inspection_stages=True,
            )
        except UserAbort:
            raise
        except InspectionBoundaryFailure as exc:
            issue = VMIssue(source=exc.source)
            issues.append(issue)
            diagnostics.append(VMDiagnostic(issue=issue, error=exc.diagnostic))
        else:
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
        raw_live_resources = _mgr._query_live_resources(vm, config)
        if raw_live_resources is not None:
            live_resources = VMLiveResources.from_mapping(raw_live_resources)

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
                    mode=project_persisted_enum(session.mode, SessionMode),
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
    return VMDescription(
        vm=VMDetailFacts.from_row(vm),
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
    )


def render_vm_description(description: VMDescription) -> None:
    """Render VM detail facts with the legacy human layout."""
    vm = description.vm
    for diagnostic in description.diagnostics:
        error = diagnostic.error
        output.warn(f"{error}" + (f"\n{error.hint}" if error.hint else ""))

    site_platform = description.platform or "-"
    backend_label = description.backend or "-"
    status_label = description.observed_status or "-"
    if description.status_disposition is not None:
        status_label += f" ({description.status_disposition})"

    output.info(f"Name:           {vm.name}")
    output.info(f"Created:        {vm.created_at}")
    output.info(f"Site:           {vm.site}")
    output.info(f"Platform:       {site_platform}")
    output.info(f"Backend:        {backend_label}")
    output.info(f"Status:         {status_label}")
    output.info(f"Hostname:       {vm.hostname}")
    # The slug never shows in normal CLI output (vm list stays
    # name-only); describe and doctor are its surfaces. The slug is
    # install-level, so a VM created before it was set gets a marker:
    # its hostname and backend names carry no prefix. A blank answer is
    # a VALID one: declined ("(none)") renders distinctly from
    # never-asked ("-").
    slug = description.system_slug
    slug_label = slug or ("(none)" if description.system_slug_state == "declined" else "-")
    # Exact hostname comparison (the slug is immutable and the hostname is
    # recorded as {slug}-{name}); a prefix test could false-negative on
    # a pre-slug VM whose name happens to start with the slug.
    if slug and vm.hostname != f"{slug}-{vm.name}":
        slug_label += " (not applied to this VM)"
    output.info(f"System Slug:    {slug_label}")
    output.info(f"Template:       {vm.template or '-'}")
    output.info(f"Admin User:     {vm.admin_username}")
    output.info(f"Provisioning:   {vm.provisioning_status}")
    output.info(f"Initialization: {vm.initialization_status}")
    output.info(f"Tailscale IP:   {vm.tailscale_host or '-'}")

    live = description.live_resources

    if vm.cpus is not None or live is not None:
        output.info(f"\n{'Resources':<16}{'Provisioned':<14}{'Current':<14}{'Used'}")
        output.detail(
            f"{'CPU':<16}"
            f"{str(vm.cpus) if vm.cpus else '-':<14}"
            f"{live.cpus if live else '-':<14}"
            f"{'load ' + live.load_average if live else '-'}"
        )
        output.detail(
            f"{'Memory':<16}"
            f"{str(vm.memory_gib) + 'G' if vm.memory_gib else '-':<14}"
            f"{live.memory_total if live else '-':<14}"
            f"{live.memory_used + ' (' + live.memory_percent + ')' if live else '-'}"
        )
        output.detail(
            f"{'Swap':<16}"
            f"{str(vm.swap_gib) + 'G' if vm.swap_gib else '-':<14}"
            f"{live.swap_total if live else '-':<14}"
            f"{live.swap_used + ' (' + live.swap_percent + ')' if live else '-'}"
        )
        output.detail(
            f"{'Disk':<16}"
            f"{str(vm.disk_gib) + 'G' if vm.disk_gib else '-':<14}"
            f"{live.disk_total if live else '-':<14}"
            f"{live.disk_used + ' (' + live.disk_percent + ')' if live else '-'}"
        )

    if vm.last_seen_at:
        output.info(f"Last Seen:      {vm.last_seen_at}")

    # Agents on this VM
    output.info(f"\nAgents ({len(description.agents)}):")
    if description.agents:
        for agent in description.agents:
            grant_count = agent.grant_count
            grant_label = "all" if agent.grant_all else str(grant_count)
            output.detail(f"{agent.name}  (user: {agent.linux_user}, grants: {grant_label})")
    else:
        output.detail("(none)")

    # Workspaces with sessions
    output.info(f"\nWorkspaces ({len(description.workspaces)}):")
    if description.workspaces:
        for ws in description.workspaces:
            output.detail(f"{ws.name}  ({ws.path})")
            # Headerless sections carry the per-workspace session listing's
            # indentation (was detail(indent=2)/detail(indent=3)): the
            # "Sessions" line sits one level under the workspace, each
            # session one level under that.
            with output.section():
                if ws.sessions:
                    output.detail(f"Sessions ({len(ws.sessions)}):")
                    with output.section():
                        for s in ws.sessions:
                            mode_label = (
                                s.mode if s.mode == "unknown" else f"agent:{s.agent_name}" if s.agent_name else "admin"
                            )
                            output.detail(f"{s.name}  [{s.template}]  {mode_label}")
                else:
                    output.detail("(no sessions)")
    else:
        output.detail("(none)")

    # Events
    output.info(f"\nEvents ({len(description.events)}):")
    if description.events:
        for event in description.events:
            evt_detail = f"  {event.detail}" if event.detail else ""
            output.detail(f"{event.created_at}  {event.event}{evt_detail}")
    else:
        output.detail("(none)")


def describe_vm(db: Database, config: Config, name: str) -> None:
    """Show VM details through the shared inspection fact record."""
    render_vm_description(vm_description(db, config, name))
