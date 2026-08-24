"""Health checks for the agentworks environment.

Returns structured results. The presentation layer decides rendering.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, assert_never

from agentworks.path_rendering import format_host_path
from agentworks.resources.access import ResourceIdentity

if TYPE_CHECKING:
    from agentworks.capabilities.secret_backend import TtyInteractionAccess
    from agentworks.config import Config
    from agentworks.machine_output import JsonObject
    from agentworks.resources.registry import Registry
    from agentworks.secrets.preview import ResolutionPreview
    from agentworks.secrets.sources import SecretSourceDecl, SourceProvenance
    from agentworks.vms.admin import AdminConfig
    from agentworks.vms.sites import VMSiteDecl


class Status(Enum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class HealthCheck:
    name: str
    status: Status
    message: str | None = None
    hint: str | None = None
    """Optional remediation text. Rendered on a separate line by the
    CLI surface so the operator sees actionable next steps without
    cramming everything into one parenthetical."""
    secret_preview: ResolutionPreview | None = None


@dataclass
class HealthGroup:
    name: str
    checks: list[HealthCheck] = field(default_factory=list)

    def _append(
        self,
        status: Status,
        name: str,
        message: str | None,
        hint: str | None,
    ) -> None:
        self.checks.append(
            HealthCheck(
                name=name,
                status=status,
                message=message,
                hint=hint,
            )
        )

    def ok(
        self,
        name: str,
        message: str | None = None,
        *,
        hint: str | None = None,
    ) -> None:
        self._append(Status.OK, name, message, hint)

    def info(
        self,
        name: str,
        message: str | None = None,
        *,
        hint: str | None = None,
    ) -> None:
        self._append(Status.INFO, name, message, hint)

    def warn(
        self,
        name: str,
        message: str | None = None,
        *,
        hint: str | None = None,
    ) -> None:
        self._append(Status.WARN, name, message, hint)

    def fail(
        self,
        name: str,
        message: str | None = None,
        *,
        hint: str | None = None,
    ) -> None:
        self._append(Status.FAIL, name, message, hint)


@dataclass
class HealthReport:
    groups: list[HealthGroup] = field(default_factory=list)

    def counts(self) -> dict[Status, int]:
        """Compute all status counts in a single pass."""
        result = {s: 0 for s in Status}
        for g in self.groups:
            for c in g.checks:
                result[c.status] += 1
        return result

    @property
    def ok_count(self) -> int:
        return self.counts()[Status.OK]

    @property
    def info_count(self) -> int:
        return self.counts()[Status.INFO]

    @property
    def warn_count(self) -> int:
        return self.counts()[Status.WARN]

    @property
    def fail_count(self) -> int:
        return self.counts()[Status.FAIL]

    @property
    def has_failures(self) -> bool:
        return self.counts()[Status.FAIL] > 0


@dataclass(frozen=True, slots=True)
class SecretSourceStatus:
    """Value-free doctor projection for one declared secret source."""

    name: str
    backend: str
    provenance: SourceProvenance
    active: bool
    enabled: bool
    not_ready_reason: str | None


def health_report_data(report: HealthReport) -> JsonObject:
    """Project a complete doctor report into the closed JSON v1 data shape."""
    counts = report.counts()
    return {
        "groups": [
            {
                "name": group.name,
                "checks": [health_check_data(check) for check in group.checks],
            }
            for group in report.groups
        ],
        "counts": {
            "ok": counts[Status.OK],
            "info": counts[Status.INFO],
            "warn": counts[Status.WARN],
            "fail": counts[Status.FAIL],
        },
    }


def health_check_data(check: HealthCheck) -> JsonObject:
    """Serialize the same check facts rendered to humans."""
    data: JsonObject = {
        "name": check.name,
        "status": check.status.value,
        "message": check.message,
        "hint": check.hint,
    }
    if check.secret_preview is not None:
        from agentworks.secrets.preview import preview_data

        data["secret_preview"] = preview_data(check.secret_preview)
    return data


def _stored_readiness_check(
    registry: Registry,
    identity: ResourceIdentity,
) -> HealthCheck | None:
    """Build doctor's stored-readiness row, or no row when disabled."""
    from agentworks.resources.graph import Enablement

    if registry.graph.enablement_of(identity.kind, identity.name) is Enablement.disabled:
        return None
    reason = registry.graph.readiness_of(identity.kind, identity.name).reason
    if reason is not None:
        return HealthCheck(identity.name, Status.INFO, f"not ready: {reason}")
    return HealthCheck(identity.name, Status.OK)


def _vm_site_check(
    config: Config,
    registry: Registry,
    name: str,
    decl: VMSiteDecl,
) -> HealthCheck:
    """Build the exact per-site row used by bulk and focused doctor views."""
    reason = registry.graph.readiness_of("vm-site", name).reason
    if reason is not None:
        return HealthCheck(name, Status.INFO, f"not ready: {reason}")
    try:
        from agentworks.capabilities.base import RunContext
        from agentworks.vms.nodes import vm_site_node

        vm_site_node(registry, name).preflight(RunContext(config=config))
    except Exception as error:
        return HealthCheck(
            name,
            Status.WARN,
            f"{_platform_summary(decl)}; preflight: {error}",
            hint=getattr(error, "hint", None),
        )
    return HealthCheck(name, Status.OK, _platform_summary(decl))


def _secret_source_check(status: SecretSourceStatus) -> HealthCheck:
    """Build one value-free source participation/readiness row."""
    from agentworks.secrets.sources import SourceProvenance

    provenance_labels = {
        SourceProvenance.OPERATOR_OVERRIDE: "operator override of synthesized default",
        SourceProvenance.SYNTHESIZED_DEFAULT: "synthesized default",
        SourceProvenance.DECLARED: "declared",
    }
    participation = "active" if status.active else "inactive"
    enablement = "enabled" if status.enabled else "disabled"
    readiness = f"not ready: {status.not_ready_reason}" if status.not_ready_reason is not None else "ready"
    return HealthCheck(
        status.name,
        Status.INFO,
        f"backend {status.backend}; {participation}; {enablement}; {provenance_labels[status.provenance]}; {readiness}",
    )


def _secret_check(
    name: str,
    preview: ResolutionPreview,
) -> HealthCheck:
    """Build one value-free secret resolution preview row."""
    from agentworks.capabilities.secret_backend import (
        IndeterminateReason,
        PreviewAvailable,
        PreviewBlocked,
        PreviewFailed,
        PreviewIndeterminate,
        PreviewMissing,
    )
    from agentworks.secrets.preview import AggregateNoCandidate, preview_hint

    label = f"Secret {name!r}"
    result = preview.result
    if isinstance(result, PreviewIndeterminate):
        if result.reason is IndeterminateReason.OPERATOR_INPUT_REQUIRED:
            return HealthCheck(
                label,
                Status.OK,
                "provided interactively when needed",
                secret_preview=preview,
            )
        if result.reason is IndeterminateReason.OPERATOR_IMPACT_LIMITED:
            return HealthCheck(
                label,
                Status.INFO,
                f"availability not checked through {preview.source}",
                secret_preview=preview,
            )
        assert_never(result.reason)
    if isinstance(result, PreviewAvailable):
        status = Status.OK
    elif isinstance(result, (PreviewMissing, PreviewBlocked, AggregateNoCandidate)):
        status = Status.WARN
    elif isinstance(result, PreviewFailed):
        status = Status.FAIL
    else:
        assert_never(result)
    reason = f"/{preview.reason}" if preview.reason is not None else ""
    return HealthCheck(
        label,
        status,
        f"{preview.status.value}{reason}; source={preview.source or 'none'}",
        hint=preview_hint(preview, interaction_opt_in=False),
        secret_preview=preview,
    )


def _admin_dotfiles_check(admin: AdminConfig) -> HealthCheck | None:
    """Build the default admin-template dotfiles row when a source exists."""
    if not admin.dotfiles_source:
        return None
    from agentworks.sources import parse_source_ref

    ref = parse_source_ref(admin.dotfiles_source)
    if ref.kind == "git" or Path(ref.path).expanduser().exists():
        return HealthCheck("Admin dotfiles", Status.OK, admin.dotfiles_source)
    return HealthCheck("Admin dotfiles", Status.WARN, f"source missing: {admin.dotfiles_source}")


def checks_for_resource(
    config: Config,
    registry: Registry,
    identity: ResourceIdentity,
    *,
    tty_access: TtyInteractionAccess,
) -> tuple[HealthCheck, ...]:
    """Return only doctor's checks whose subject is ``identity``."""
    from agentworks.resources.access import resolve_resource
    from agentworks.secrets.policy import require_exact_tty_interaction_access

    require_exact_tty_interaction_access(tty_access)

    row = resolve_resource(registry, identity).resource
    check: HealthCheck | None = None
    if identity.kind in {"vm-platform", "secret-backend"}:
        check = _stored_readiness_check(registry, identity)
    elif identity.kind == "vm-site":
        from agentworks.vms.sites import VMSiteDecl

        if not isinstance(row, VMSiteDecl):
            raise AssertionError("vm-site published an unexpected row type")
        check = _vm_site_check(config, registry, identity.name, row)
    elif identity.kind == "secret-source":
        from agentworks.secrets.sources import SecretSourceDecl

        if not isinstance(row, SecretSourceDecl):
            raise AssertionError("secret-source published an unexpected row type")
        check = _secret_source_check(_secret_source_status(config, registry, identity.name, row))
    elif identity.kind == "secret":
        from agentworks.capabilities.secret_backend import OperatorImpact
        from agentworks.secrets.base import SecretDecl
        from agentworks.secrets.preview import preview_batch
        from agentworks.secrets.resolve import active_sources

        if not isinstance(row, SecretDecl):
            raise AssertionError("secret published an unexpected row type")
        preview = preview_batch(
            [row],
            active_sources(config, registry),
            impact=OperatorImpact.NONE,
            tty_access=tty_access,
            interaction_broker=None,
        )[row.name]
        check = _secret_check(identity.name, preview)
    elif identity == ResourceIdentity("admin-template", "default"):
        from agentworks.vms.admin import AdminConfig

        if not isinstance(row, AdminConfig):
            raise AssertionError("admin-template published an unexpected row type")
        check = _admin_dotfiles_check(row)
    return () if check is None else (check,)


def run_checks(
    *,
    tty_access: TtyInteractionAccess,
    completion_version: str | None = None,
) -> HealthReport:
    """Run all health checks and return structured results.

    Args:
        tty_access: exact caller-supplied terminal access for secret preview.
        completion_version: current completion spec version for staleness check.
            Computed by the CLI layer and passed in to avoid coupling doctor
            to the CLI module. Omit to skip completion checks.
    """
    from agentworks.secrets.policy import require_exact_tty_interaction_access

    require_exact_tty_interaction_access(tty_access)
    report = HealthReport()

    # Group order is a presentation choice, decoupled from which checks
    # need config: the config/registry pair loads up front and each
    # dependent group renders wherever it reads best. Identity first,
    # then the environment, then the VM stack, then everything the
    # config graph drives.
    config_group, config, registry = _check_config()

    report.groups.append(_check_system())
    report.groups.append(_check_python())
    report.groups.append(_check_required_tools())
    report.groups.append(_check_tailscale())
    # System plugins leads the config-driven groups: it is a fundamental opt-in
    # (it determines which platforms, backends, and harness integrations even exist), so it
    # reads best up front, before the VM stack it shapes, rather than splitting VM
    # platforms from VM sites. The roster reads config.enabled_system_plugins
    # against SYSTEM_PLUGINS and needs no registry (a plugin is an origin, not a
    # resource kind, R12), so it skips only when config is unavailable. The None
    # checks are spelled out at each site (not hoisted into a boolean local)
    # because mypy's narrowing does not flow through one.
    if config is not None:
        report.groups.append(_check_plugins(config))
    else:
        report.groups.append(_skipped_group("System plugins", "Installed plugins"))
    # VM platforms and VM sites render adjacent. VM platforms read stored
    # readiness off the graph (R11), so they need the registry and skip cleanly
    # in degraded mode like the others.
    if registry is not None:
        report.groups.append(_check_vm_platforms(registry))
    else:
        report.groups.append(_skipped_group("VM platforms", "Installed platforms"))
    if config is not None and registry is not None:
        report.groups.append(_check_vm_sites(config, registry))
    else:
        report.groups.append(_skipped_group("VM sites", "Declared sites"))
    report.groups.append(config_group)
    if registry is not None:
        report.groups.append(_check_secret_backends(registry))
    else:
        report.groups.append(_skipped_group("Secret backends", "Registered backends"))
    if config is not None and registry is not None:
        report.groups.append(_check_secret_sources(config, registry))
    else:
        report.groups.append(_skipped_group("Secret sources", "Declared sources"))
    if config is not None and registry is not None:
        report.groups.append(_check_secrets(config, registry, tty_access=tty_access))
    else:
        report.groups.append(_skipped_group("Secrets", "Declared secrets"))
    report.groups.append(_check_database())

    if completion_version is not None:
        report.groups.append(_check_completions(completion_version))

    return report


# ---------------------------------------------------------------------------
# Individual check groups
# ---------------------------------------------------------------------------


def _skipped_group(name: str, item: str) -> HealthGroup:
    """Render a group that cannot run because config or manifests are
    unavailable (degraded mode).

    A config/registry-dependent group renders in presentation order,
    before the Configuration group that explains the failure. Omitting it
    entirely would read as a real (empty) result: "no sites", "no
    secrets". So it renders one explicit skip row instead, giving an
    operator comparing a healthy run to a degraded one a clear signal
    that the group was not checked and where to look for why. Any future
    config-dependent group routes its degraded case through here so it
    gets the same visible skip for free.
    """
    g = HealthGroup(name)
    g.info(
        item,
        "skipped (config or manifests unavailable; see the Configuration group)",
    )
    return g


def _check_system() -> HealthGroup:
    """Install-level identity: the system slug. Not a VM-site concern
    (it namespaces hostnames, backend-side names, and the managed SSH
    config file install-wide), so it leads the report under its own
    header rather than hiding in the VM groups.
    """
    from agentworks.doctor_state import check_system

    return check_system()


def _check_python() -> HealthGroup:
    g = HealthGroup("Python")
    v = sys.version_info
    if v >= (3, 12):
        g.ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        g.fail(f"Python {v.major}.{v.minor}.{v.micro}", "3.12+ required")
    return g


def _check_required_tools() -> HealthGroup:
    g = HealthGroup("Required tools")
    for tool in ("ssh", "scp", "tailscale"):
        if shutil.which(tool):
            g.ok(tool)
        else:
            g.fail(tool, "not found")
    return g


def _check_vm_platforms(registry: Registry) -> HealthGroup:
    """Installed platforms and their host support, read from each
    ``vm-platform`` row's stored readiness verdict off the graph (R11: the
    finalize fold computed it, doctor does not recompute or probe the live
    registry): a supported platform is ``ok``; a host-unsupported one shows
    ``not ready: <reason>``. Per-site availability (a local-Lima site without
    ``limactl``) is the SITE's state and reports in the VM sites group.

    A DISABLED platform (its opt-in axis reads ``Enablement.disabled``, e.g. a
    plugin platform whose plugin is not enabled) is SKIPPED here: the "System
    plugins" roster is the enablement authority and already lists it as
    disabled, so folding it in would double-list it and, worse, render its
    ready-placeholder readiness as a misleading ``[ok]``. This matches the
    "disabled hides" default-surface rule that ``agw resource list`` uses.
    """
    from agentworks.resources.access import ResourceIdentity

    g = HealthGroup("VM platforms")
    # Publication order (built-ins in name order, then plugin rows in
    # plugin-publication order), preserved: the pre-refactor group rendered in
    # this order, and the reordering is not one of the R9 deltas.
    for name, _decl in registry.iter_kind_items("vm-platform"):
        check = _stored_readiness_check(registry, ResourceIdentity("vm-platform", name))
        if check is not None:
            g.checks.append(check)
    return g


def _check_plugins(config: Config) -> HealthGroup:
    """The system-plugin roster (R9, R10, R12): every installed plugin, its
    description, and its opt-in state, read from ``SYSTEM_PLUGINS`` against
    ``config.enabled_system_plugins``.

    A BESPOKE surface, not a ``KIND_REGISTRY``-dispatched hook: a plugin is an
    origin, not a resource kind (R12). Roster only (existence, description,
    enable-state); it never enumerates a disabled plugin's contributed
    capabilities or resources (that is what the enablement axis and the
    reference hint are for). The reserved ``required_scopes`` (R10) render as an
    informational least-privilege line when populated, unenforced; empty (the
    v1 default) renders nothing. When the shipped index is empty the group
    renders empty-but-present, so the surface exists and is testable even before
    any plugin ships; the migrated plugins (``onepassword``, ``claude``,
    ``proxmox``, ``azure``) now populate it.
    """
    from agentworks.plugins import SYSTEM_PLUGINS

    g = HealthGroup("System plugins")
    if not SYSTEM_PLUGINS:
        g.info("No system plugins installed.")
        return g

    enabled = set(config.enabled_system_plugins)
    for name, plugin in sorted(SYSTEM_PLUGINS.items()):
        if name in enabled:
            g.ok(f"plugin {name}", plugin.description or None)
        else:
            # The doctor renders the "not enabled in [plugins].system" STATE
            # phrasing (the enablement mark carries only the "enable plugin
            # <name>" remediation clause, never this state string).
            message = "disabled (not enabled in [plugins].system)"
            if plugin.description:
                message = f"{message}; {plugin.description}"
            g.info(f"plugin {name}", message)
        if plugin.required_scopes:
            levels = ", ".join(level.value for level in plugin.required_scopes)
            g.info(f"plugin {name} least privilege", levels)
    return g


def _platform_summary(decl: VMSiteDecl) -> str:
    """The site row's platform clause: the platform's name plus the
    resolved tag of each of its mode unions, e.g.
    ``platform lima (placement: local)``.

    The modes render here because they can be IMPLICIT now: the unions
    carry declared defaults (azure and aws's ``auth``, lima's
    ``placement``), so a site that wrote nothing has still resolved to
    an arm, and this row is where a reviewer checks a fleet's sites
    without opening manifests. Total: a site whose modes cannot be read
    renders the bare platform name it always did.
    """
    from agentworks.capabilities.config import resolved_capability_modes

    modes = resolved_capability_modes(kind="vm-platform", config=decl.platform.tagged)
    summary = f"platform {decl.platform.name}"
    if modes:
        summary += " (" + ", ".join(f"{field}: {tag}" for field, tag in modes) + ")"
    return summary


def _check_vm_sites(
    config: Config,
    registry: Registry,
) -> HealthGroup:
    """VM sites: every registered site's state, and every VM's site
    resolving to a usable declaration.

    A NOT-READY site (its stored readiness verdict: platform
    host-unsupported, a disabled platform, or a missing local requirement) is
    informational (normal for the host, the site still exists) and
    skips preflight (pointless without its requirements). References
    to a not-ready site are the operator's problem-in-waiting and warn:
    ``defaults.site`` and each VM row pointing at one. A VM whose site
    is not declared at all still FAILS with the paste-ready manifest
    snippet (the stranded remote-Lima case).

    A ready site's row IS the site node's ``preflight``: its declared
    secrets reaching real registry rows, plus the held platform
    instance's world checks. Read-only by contract, which is exactly
    what lets doctor call it.

    What that row does NOT cover is whether the site's declared secrets
    have an attemptable source. That prediction belongs to an operation's runtime
    world (which sources are active, whether this run can prompt), so
    it lives in the operation's preflight sweep
    (:func:`~agentworks.orchestration.readiness.preflight_all`), which
    doctor does not run: doctor invokes ``node.preflight`` per row,
    deliberately. Resolvability renders once, on the secret's own row in
    the Secrets group, instead of being smeared across every resource
    that names it, and a site whose credential is prompt-only reads ok
    here, correctly, because nothing about that site is unhealthy.
    """
    from agentworks.vms.sites import VMSiteDecl

    g = HealthGroup("VM sites")

    sites: dict[str, VMSiteDecl] = {}
    for name, decl in registry.iter_kind_items("vm-site"):
        assert isinstance(decl, VMSiteDecl)
        sites[name] = decl
    not_ready: dict[str, str] = {}
    for name in sorted(sites):
        decl = sites[name]
        reason = registry.graph.readiness_of("vm-site", name).reason
        if reason is not None:
            not_ready[name] = reason
        g.checks.append(_vm_site_check(config, registry, name, decl))

    default_site = config.defaults.site
    if default_site is not None and default_site in not_ready:
        g.warn(
            "defaults.site",
            f"names '{default_site}', which is not ready: {not_ready[default_site]}",
        )

    from agentworks.doctor_state import append_vm_site_database_checks

    append_vm_site_database_checks(g, sites=sites, not_ready=not_ready)
    return g


def _check_tailscale() -> HealthGroup:
    """WORKSTATION Tailscale state only: is this machine connected to
    the tailnet? Binary presence is Required tools' row; the auth key
    is an ordinary secret and reports in the Secrets group like any
    other (`agw secret describe tailscale-auth-key` for detail).
    """
    g = HealthGroup("Tailscale")
    if not shutil.which("tailscale"):
        # Required tools already fails the missing binary; nothing to
        # add here without it.
        g.info("Connectivity", "skipped (tailscale not installed)")
        return g

    try:
        result = subprocess.run(
            ["tailscale", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            g.ok("Connected to tailnet")
        else:
            g.fail("Not connected", "run 'tailscale up'")
    except subprocess.TimeoutExpired:
        g.fail("tailscale status", "timed out")
    return g


def _check_config() -> tuple[HealthGroup, Config | None, Registry | None]:
    """Return config and registry facts for the complete doctor report."""
    from agentworks.config import CONFIG_PATH, ConfigError
    from agentworks.errors import ValidationError

    g = HealthGroup("Configuration")
    config = None

    # Home-relative like every other path doctor prints. Doctor is the
    # command whose output gets pasted into an issue, which is the one
    # argument for spelling paths absolutely here, and it does not hold
    # up: an operator's home is not the ambiguous part of a bug report,
    # and `~/` avoids pasting their username into a public tracker. The
    # decisive point is that this row and the Manifest row render a few
    # lines apart in the same group, naming files in the same directory
    # tree. Whatever they do they must do together, and the Manifest row
    # is framed by `located()`, which is shared with every non-doctor
    # surface that reports the same error, so it is not doctor's to change.
    if not CONFIG_PATH.exists():
        g.fail(
            "Config file",
            f"not found: {format_host_path(CONFIG_PATH)}. Run 'agw config init' to create one.",
        )
        return g, None, None

    g.ok("Config file", format_host_path(CONFIG_PATH))

    try:
        from agentworks.config import load_config

        config = load_config(warn_issues=False, raise_errors=True, workload_gated_issues_fatal=False)
    except (ConfigError, ValidationError) as e:
        # ValidationError is a SIBLING of ConfigError under AgentworksError,
        # not a subclass, so it must be named explicitly. Catching it here
        # yields a fail row and lets the rest of the report render, per
        # doctor's maximal-visibility contract, instead of aborting with a
        # bare one-liner and no report.
        g.fail(
            "Config",
            str(e),
            hint=e.hint,
        )
        return g, None, None
    # Manifest spec-level warnings (unknown keys with file:line, env
    # hygiene, ...) surface as doctor rows, exactly like TOML
    # config_issues below. Loading here (and passing the set into
    # build_registry) also keeps build_registry's auto-load from
    # printing ambient warnings above the report. Doctor rows are the
    # surface. A typo'd key on a manifest-declared resource previously
    # warned ambiently while the Config row said ok.
    from agentworks.manifests import RESOURCES_DIRNAME, load_manifests

    # A manifest load failure gets its fail row but does NOT short-circuit
    # the report: the TOML issue rows, deprecation rows, and SSH checks
    # below still render (doctor's job is maximal visibility in one run);
    # only the registry-dependent tail is skipped.
    resources_dir = config.source_path.parent / RESOURCES_DIRNAME
    manifests = None
    try:
        manifests = load_manifests(resources_dir)
    except (ConfigError, ValidationError) as e:
        # Same sibling-miss guard as the config load above: the manifest
        # decode path runs validate_name (e.g. _decode_vm_site, _decode_secret)
        # and so can surface a ValidationError. Caught here it becomes a fail
        # row and the rest of the report (TOML issues, SSH checks, ...) still
        # renders, rather than aborting the whole run.
        g.fail(
            "Manifest",
            str(e),
            hint=e.hint,
        )

    for issue in config.config_issues:
        g.warn("Config", issue)
    if manifests is not None:
        for issue in manifests.issues:
            g.warn("Manifest", issue)
    if not config.config_issues and manifests is not None and not manifests.issues:
        g.ok("Config is valid")
    # No deprecation rows here: every config.toml deprecation doctor used to
    # render is retired now, so stale inputs reach ordinary Config or Manifest
    # validation instead. If a nudge is added back to ``Config.deprecation_issues``,
    # render it here as a scannable one-liner (maintainer ruling, 2026-07-06):
    # the FACT plus one next step, with the teaching text left on the ambient
    # per-command warning.

    # SSH keys
    _check_ssh_key(g, config.operator.ssh_public_key, "public")
    _check_ssh_key(g, config.operator.ssh_private_key, "private")

    # Resource registry (framework validation: references, miss
    # policies, cycles). A failure here is a config problem, reported
    # like any other; the resource-dependent checks below are skipped.
    from agentworks.bootstrap import build_registry

    if manifests is None:
        return g, config, None

    try:
        registry = build_registry(config, manifests=manifests)
    except ConfigError as e:
        g.fail(
            "Resource registry",
            str(e),
            hint=e.hint,
        )
        return g, config, None

    # Dotfiles
    from agentworks.resources.access import admin_template

    admin = admin_template(registry)
    dotfiles_check = _admin_dotfiles_check(admin)
    if dotfiles_check is not None:
        g.checks.append(dotfiles_check)

    # Git token health is preflight-only in doctor: the token secret's
    # resolvability shows in the Secrets group (`_check_secrets` covers
    # the git-token-* family) like any other secret. Live authenticated
    # verification (expired/revoked/wrong-scope) is the capability
    # `runup()` stage, which runs inside provisioning ops; on-demand
    # authenticated checking is the deferred `doctor --runup` (issue
    # #176). Doctor never prompts, so an authenticated check here could
    # only ever reach non-interactively-resolvable secrets, forking
    # readiness on where a secret comes from: the asymmetry we reject.

    return g, config, registry


def _check_ssh_key(g: HealthGroup, path: object, label: str) -> None:
    """Check that an SSH key file exists and has correct permissions."""
    if not isinstance(path, Path):
        g.fail(f"SSH {label} key", "invalid path")
        return
    if not path.exists():
        g.fail(f"SSH {label} key", f"not found: {format_host_path(path)}")
        return
    if not os.access(path, os.R_OK):
        g.fail(f"SSH {label} key", f"not readable: {format_host_path(path)}")
        return

    g.ok(f"SSH {label} key", format_host_path(path))

    # Check permissions on private key. Skipped on Windows: st_mode there is
    # synthesized from the read-only attribute (typically reports 0o666) and
    # doesn't reflect the NTFS ACLs that actually gate access.
    if label == "private" and sys.platform != "win32":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            g.warn("SSH private key permissions", f"{oct(mode)}, recommend 600")


def _check_secret_backends(registry: Registry) -> HealthGroup:
    """Every registered secret-backend's stored readiness verdict off the graph
    (R11), parallel to ``_check_vm_platforms``: a backend usable here is ``ok``;
    a backend whose host tool is missing (e.g. ``onepassword`` with no ``op`` on
    PATH) is ``not ready: <reason>``.

    This is offline host readiness only (no store probe, no biometric); whether a
    specific secret resolves is ``_check_secrets``, and enablement (opt-in) /
    chain membership are ``agw secret describe`` / ``agw secret list`` concerns,
    not this per-backend readiness sweep (R9.7's promised backend visibility).

    A DISABLED backend (its opt-in axis reads ``Enablement.disabled``, e.g. a
    plugin backend like ``onepassword`` whose plugin is not enabled) is SKIPPED
    here, parallel to ``_check_vm_platforms``: the "System plugins" roster is the
    enablement authority and already lists it as disabled, so folding it in would
    double-list it and render its ready-placeholder readiness as a misleading
    ``[ok]``. This matches the "disabled hides" default-surface rule that
    ``agw resource list`` uses.
    """
    from agentworks.resources.access import ResourceIdentity

    g = HealthGroup("Secret backends")
    backends = sorted(registry.iter_kind_items("secret-backend"), key=lambda item: item[0])
    if not backends:
        g.info("Registered backends", "none")
        return g
    for name, _decl in backends:
        check = _stored_readiness_check(registry, ResourceIdentity("secret-backend", name))
        if check is not None:
            g.checks.append(check)
    return g


def _secret_source_status(
    config: Config,
    registry: Registry,
    name: str,
    row: SecretSourceDecl,
) -> SecretSourceStatus:
    from agentworks.resources.graph import Enablement
    from agentworks.secrets.sources import source_provenance

    return SecretSourceStatus(
        name=name,
        backend=row.backend.name,
        provenance=source_provenance(row),
        active=name in config.secret_config_data.sources,
        enabled=registry.graph.enablement_of("secret-source", name) is not Enablement.disabled,
        not_ready_reason=registry.graph.readiness_of("secret-source", name).reason,
    )


def _secret_source_statuses(config: Config, registry: Registry) -> tuple[SecretSourceStatus, ...]:
    """Project every source row without invoking a capability operation."""
    from agentworks.secrets.sources import SecretSourceDecl

    statuses: list[SecretSourceStatus] = []
    for name, row in sorted(registry.iter_kind_items("secret-source"), key=lambda item: item[0]):
        if not isinstance(row, SecretSourceDecl):
            continue
        statuses.append(_secret_source_status(config, registry, name, row))
    return tuple(statuses)


def _check_secret_sources(config: Config, registry: Registry) -> HealthGroup:
    """Explain participation and readiness for every declared source row."""
    group = HealthGroup("Secret sources")
    statuses = _secret_source_statuses(config, registry)
    if not statuses:
        group.info("Declared sources", "none")
        return group
    for status in statuses:
        group.checks.append(_secret_source_check(status))
    return group


def _check_secrets(
    config: Config,
    registry: Registry,
    *,
    tty_access: TtyInteractionAccess,
) -> HealthGroup:
    """Preview each declared secret under caller-supplied TTY access."""
    from agentworks.capabilities.secret_backend import OperatorImpact
    from agentworks.resources.access import secret_decls
    from agentworks.secrets.preview import preview_batch

    g = HealthGroup("Secrets")

    secrets = secret_decls(registry)
    if not secrets:
        g.info("Declared secrets", "none")
        return g

    from agentworks.secrets.resolve import active_sources

    ordered = tuple(sorted(secrets.items()))
    previews = preview_batch(
        [decl for _name, decl in ordered],
        active_sources(config, registry),
        impact=OperatorImpact.NONE,
        tty_access=tty_access,
        interaction_broker=None,
    )
    for name, decl in ordered:
        g.checks.append(_secret_check(name, previews[decl.name]))

    return g


def _check_database() -> HealthGroup:
    from agentworks.doctor_state import check_database

    return check_database()


def _check_completions(current_version: str) -> HealthGroup:
    g = HealthGroup("Shell completions")

    shells = _get_completion_paths()

    any_found = False
    for shell_name, candidate_paths in shells:
        existing = [p for p in candidate_paths if p.exists()]
        if not existing:
            continue
        any_found = True
        # Completions may linger under this home from a prior install or a
        # synced home dir even when the shell itself isn't present here (e.g.
        # bash/zsh files on a Windows box driven from PowerShell). Don't nag
        # about staleness for a shell that can't run here; report it and move
        # on so the results tally stays clean.
        if not _shell_available(shell_name):
            g.info(shell_name, f"completions installed, but {shell_name} not found on this machine")
            continue
        for path in existing:
            installed_version = _read_completion_version(path)
            if installed_version == current_version:
                g.ok(shell_name, "up to date")
            elif installed_version is None:
                g.warn(shell_name, f"no version stamp. Re-run: agw completion install --shell {shell_name}")
            else:
                g.warn(shell_name, f"stale. Re-run: agw completion install --shell {shell_name}")
    if not any_found:
        g.ok(
            "Completions",
            "none installed (install with: agw completion install [--shell <bash|zsh|powershell>])",
        )

    return g


def _shell_available(shell_name: str) -> bool:
    """Whether the shell can actually run on this machine (found on PATH).

    PowerShell ships as either `pwsh` (Core) or `powershell` (Windows
    PowerShell), so either binary counts as the `powershell` shell being
    present.

    Kept in sync with ``_get_completion_paths``: today its PowerShell entry
    only exists when ``_query_powershell_profile`` finds a binary on PATH,
    so the powershell branch here can't fire in practice. If that
    enumeration ever changes to include a static PowerShell path, this
    branch becomes load-bearing.
    """
    candidates = {"powershell": ("pwsh", "powershell")}.get(shell_name, (shell_name,))
    return any(shutil.which(c) for c in candidates)


def _get_completion_paths() -> list[tuple[str, list[Path]]]:
    """Return (shell_name, candidate_paths) for all shells."""
    home = Path.home()
    shells: list[tuple[str, list[Path]]] = []

    # Bash
    shells.append(
        (
            "bash",
            [home / ".local" / "share" / "bash-completion" / "completions" / "agentworks"],
        )
    )

    # Zsh
    zsh_paths: list[Path] = [home / ".zfunc" / "_agentworks"]
    zsh_custom = os.environ.get("ZSH_CUSTOM")
    if zsh_custom:
        zsh_paths.append(Path(zsh_custom) / "completions" / "_agentworks")
    omz_default = home / ".oh-my-zsh" / "custom" / "completions" / "_agentworks"
    if omz_default not in zsh_paths:
        zsh_paths.append(omz_default)
    shells.append(("zsh", zsh_paths))

    # PowerShell
    from agentworks.completions.install import _query_powershell_profile

    profile = _query_powershell_profile()
    if profile is not None:
        shells.append(
            (
                "powershell",
                [profile.parent / "Completions" / "agentworks.ps1"],
            )
        )

    return shells


def _read_completion_version(path: Path) -> str | None:
    """Read the version stamp from a completion file."""
    try:
        with path.open() as f:
            for line in f:
                if line.startswith("# agentworks-completion-version:"):
                    return line.split(":", 1)[1].strip()
                if not line.startswith("#") and line.strip():
                    break
    except OSError:
        pass
    return None
