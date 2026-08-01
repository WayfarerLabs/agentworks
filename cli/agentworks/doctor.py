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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.resources.registry import Registry


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


@dataclass
class HealthGroup:
    name: str
    checks: list[HealthCheck] = field(default_factory=list)

    def ok(self, name: str, message: str | None = None, *, hint: str | None = None) -> None:
        self.checks.append(HealthCheck(name=name, status=Status.OK, message=message, hint=hint))

    def info(self, name: str, message: str | None = None, *, hint: str | None = None) -> None:
        self.checks.append(HealthCheck(name=name, status=Status.INFO, message=message, hint=hint))

    def warn(self, name: str, message: str | None = None, *, hint: str | None = None) -> None:
        self.checks.append(HealthCheck(name=name, status=Status.WARN, message=message, hint=hint))

    def fail(self, name: str, message: str | None = None, *, hint: str | None = None) -> None:
        self.checks.append(HealthCheck(name=name, status=Status.FAIL, message=message, hint=hint))


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


def run_checks(*, completion_version: str | None = None) -> HealthReport:
    """Run all health checks and return structured results.

    Args:
        completion_version: current completion spec version for staleness check.
            Computed by the CLI layer and passed in to avoid coupling doctor
            to the CLI module. Omit to skip completion checks.
    """
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
    # (it determines which platforms, backends, and harnesses even exist), so it
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
        report.groups.append(_check_secrets(config, registry))
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
    from agentworks.db import SYSTEM_SLUG_KEY, Database

    g = HealthGroup("System")
    try:
        db_exists, current, latest = Database.check_schema()
        if not db_exists:
            # No database means nothing has ever set the slug.
            g.info("System slug", "unset (will ask at first vm create)")
            return g
        if current != latest:
            # Opening the DB would auto-migrate mid-report; defer to the
            # Database group's deliberate migration row.
            g.info(
                "System slug",
                "pending database migration (see the Database group)",
            )
            return g
        db = Database()
        try:
            slug = db.get_setting(SYSTEM_SLUG_KEY)
        finally:
            db.close()
        if slug:
            g.ok("System slug", slug)
        elif slug == "":
            g.info("System slug", "declined (asked at first vm create)")
        else:
            g.info("System slug", "unset (will ask at first vm create)")
    except Exception as e:
        g.warn("System slug", f"could not check the database: {e}")
    return g


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
    from agentworks.resources.graph import Enablement

    g = HealthGroup("VM platforms")
    # Registry-definition order (what ``publish_to`` established from
    # ``VM_PLATFORM_REGISTRY``), preserved: the pre-refactor group rendered in
    # this order, and the reordering is not one of the R9 deltas.
    for name, _decl in registry.iter_kind_items("vm-platform"):
        if registry.graph.enablement_of("vm-platform", name) is Enablement.disabled:
            continue
        reason = registry.graph.readiness_of("vm-platform", name).reason
        if reason is not None:
            g.info(name, f"not ready: {reason}")
        else:
            g.ok(name)
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


def _check_vm_sites(config: Config, registry: Registry) -> HealthGroup:
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
    would RESOLVE. That prediction belongs to an operation's runtime
    world (which backends are active, whether this run can prompt), so
    it lives in the operation's preflight sweep
    (:func:`~agentworks.orchestration.readiness.preflight_all`), which
    doctor does not run: doctor invokes ``node.preflight`` per row,
    deliberately. Resolvability renders once, on the secret's own row in
    the Secrets group, instead of being smeared across every resource
    that names it, and a site whose credential is prompt-only reads ok
    here, correctly, because nothing about that site is unhealthy.
    """
    from agentworks.db import Database
    from agentworks.vms.sites import (
        VMSiteDecl,
        site_manifest_hint,
    )

    g = HealthGroup("VM sites")

    sites: dict[str, VMSiteDecl] = {}
    for name, decl in registry.iter_kind_items("vm-site"):
        assert isinstance(decl, VMSiteDecl)
        sites[name] = decl
    not_ready: dict[str, str] = {}
    for name in sorted(sites):
        decl = sites[name]
        # Read the site's stored readiness verdict off the graph (R11), no
        # recompute. A site whose platform is disabled (the opt-in axis) or
        # host-unsupported reads not-ready here too.
        reason = registry.graph.readiness_of("vm-site", name).reason
        if reason is not None:
            not_ready[name] = reason
            g.info(name, f"not ready: {reason}")
            continue
        try:
            from agentworks.capabilities.base import RunContext
            from agentworks.vms.nodes import vm_site_node

            site_node = vm_site_node(registry, name)
            site_node.preflight(RunContext(config=config))
        except Exception as e:
            # A failing preflight on an enabled site is the error the
            # operator's next command hits: warn.
            g.warn(
                name,
                f"platform {decl.platform}; preflight: {e}",
                hint=getattr(e, "hint", None),
            )
            continue
        g.ok(name, f"platform {decl.platform}")

    default_site = config.defaults.site
    if default_site is not None and default_site in not_ready:
        g.warn(
            "defaults.site",
            f"names '{default_site}', which is not ready: {not_ready[default_site]}",
        )

    try:
        db_exists, current, latest = Database.check_schema()
        if not db_exists:
            # No VMs recorded yet; nothing to cross-check.
            return g
        if current != latest:
            # Opening the DB would auto-migrate mid-report (interleaving
            # the migration's own output into this group and stealing
            # the Database group's deliberate migration row); defer.
            g.info(
                "VM sites",
                "pending database migration (see the Database group); "
                "re-run doctor after migrating for the full report",
            )
            return g
        db = Database()
        try:
            for vm in db.list_vms():
                if vm.site in not_ready:
                    g.warn(
                        f"VM '{vm.name}'",
                        f"site '{vm.site}' is not ready: {not_ready[vm.site]}",
                    )
                elif vm.site not in sites:
                    g.fail(
                        f"VM '{vm.name}'",
                        f"site '{vm.site}' is not declared",
                        hint=site_manifest_hint(vm.site),
                    )
        finally:
            db.close()
    except Exception as e:
        g.warn("VM sites", f"could not check the database: {e}")
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
    """Returns (group, config_or_none, registry_or_none)."""
    from agentworks.config import CONFIG_PATH, ConfigError
    from agentworks.errors import ValidationError

    g = HealthGroup("Configuration")
    config = None

    if not CONFIG_PATH.exists():
        g.fail("Config file", f"not found: {CONFIG_PATH}. Run 'agw config init' to create one.")
        return g, None, None

    g.ok("Config file", str(CONFIG_PATH))

    try:
        from agentworks.config import load_config

        config = load_config(warn_issues=False)
    except (ConfigError, ValidationError) as e:
        # ValidationError is a SIBLING of ConfigError under AgentworksError,
        # not a subclass, so it must be named explicitly: the secrets loader
        # (_load_secrets -> validate_name) raises it for a non-conforming
        # explicit [secrets.*] name. Catching it here yields a fail row and
        # lets the rest of the report render, per doctor's maximal-visibility
        # contract, instead of aborting with a bare one-liner and no report.
        g.fail("Config", str(e), hint=e.hint)
        return g, None, None
    except SystemExit:
        g.fail("Config", "failed to load")
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
        g.fail("Manifest", str(e), hint=e.hint)

    # Deprecated-field usage (FRD R11) is surfaced as its own proactive
    # finding: warn-level fields load with a notice and are ignored, so
    # without this they only emit an easily-missed ambient log line.
    # These notices match the strings the loader puts in
    # ``manifests.issues`` verbatim, so they are pulled out of the generic
    # Manifest rows below rather than reported twice.
    from agentworks.manifests.deprecated_fields import manifest_deprecation_notices

    deprecated_field_notices = manifest_deprecation_notices(resources_dir)

    for issue in config.config_issues:
        g.warn("Config", issue)
    if manifests is not None:
        deprecated_set = set(deprecated_field_notices)
        for issue in manifests.issues:
            if issue in deprecated_set:
                continue
            g.warn("Manifest", issue)
    for notice in deprecated_field_notices:
        g.warn("Deprecated manifest field", notice)
    if not config.config_issues and manifests is not None and not manifests.issues:
        g.ok("Config is valid")
    # Deprecation nudges ride their own channel (so --no-deprecations
    # can silence the ambient per-command warning), but doctor is the
    # explicit full-health surface. Doctor rows are scannable one-liners
    # (maintainer ruling, 2026-07-06): render the FACT with one next
    # step; the full teaching text (sample pointer, silencer flag,
    # removal forecast) stays on the ambient command warning.
    if config.deprecated_sections:
        g.warn(
            "Config has deprecated TOML resource declarations",
            "migrate to YAML with `agw resource migrate`",
        )
    for section in config.noop_secret_backend_sections:
        g.warn(
            f"Config has a no-op {section} section",
            "deprecated and ignored; remove it, or `agw resource migrate --all` drops it",
        )

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
        g.fail("Resource registry", str(e), hint=e.hint)
        return g, config, None

    # Dotfiles
    from agentworks.resources.access import admin_template

    admin = admin_template(registry)
    if admin.dotfiles_source:
        from agentworks.sources import parse_source_ref

        ref = parse_source_ref(admin.dotfiles_source)
        if ref.kind == "git" or Path(ref.path).expanduser().exists():
            g.ok("Admin dotfiles", admin.dotfiles_source)
        else:
            g.warn("Admin dotfiles", f"source missing: {admin.dotfiles_source}")

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
        g.fail(f"SSH {label} key", f"not found: {path}")
        return
    if not os.access(path, os.R_OK):
        g.fail(f"SSH {label} key", f"not readable: {path}")
        return

    g.ok(f"SSH {label} key", str(path))

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
    from agentworks.resources.graph import Enablement

    g = HealthGroup("Secret backends")
    backends = sorted(registry.iter_kind_items("secret-backend"), key=lambda item: item[0])
    if not backends:
        g.info("Registered backends", "none")
        return g
    for name, _decl in backends:
        if registry.graph.enablement_of("secret-backend", name) is Enablement.disabled:
            continue
        reason = registry.graph.readiness_of("secret-backend", name).reason
        if reason is not None:
            g.info(name, f"not ready: {reason}")
        else:
            g.ok(name)
    return g


def _check_secrets(config: Config, registry: Registry) -> HealthGroup:
    """Check every registry secret for a runtime-resolvable value.

    One row per secret -- operator-declared AND auto-declared alike
    (the auto-declared ones, e.g. ``tailscale-auth-key`` and the
    ``git-token-*`` family, are exactly the secrets most likely to
    prompt or fail at command time, so a doctor that hides them cannot
    predict the next command). Auto-declared rows carry an ``(auto)``
    marker.

    - OK: at least one active backend in the chain would resolve the
      secret at runtime (the message says which -- "would resolve via
      prompt" is the heads-up that a prompt is coming).
    - WARN: no active backend would resolve it (config is valid but
      there's no path to a value -- e.g. env-var has no matching env
      var set and prompt is opted out, or the only attempting backend is
      not-ready on this host so resolution would skip it, R9.6).
    - FAIL: the secret's ``backend_mappings`` references an unknown
      backend name. Config error; nothing to resolve against. FAIL
      takes precedence over OK / WARN so the operator fixes the typo
      before we tell them about resolution.

    Backend-applicability detail (per-backend soft-skip reasons,
    inactive mappings) lives in ``agw secret list``; unused declarations
    surface in ``agw secret describe``'s ``Referenced by:`` section.
    Doctor stays one row per secret so the summary line stays scannable.
    """
    from agentworks.resources.access import kind_dict, secret_decls

    g = HealthGroup("Secrets")

    secrets = secret_decls(registry)
    if not secrets:
        g.info("Declared secrets", "none")
        return g

    # The registry always carries the built-in env-var / prompt backend
    # rows, so this set covers built-ins and manifest declarations both.
    known_backends = set(kind_dict(registry, "secret-backend").keys())
    from agentworks.secrets.resolve import active_backends, preview_resolution

    backends = active_backends(config, registry)

    for name, decl in sorted(secrets.items()):
        auto = getattr(decl.origin, "variant", None) == "auto-declared"
        label = f"Secret {name!r} (auto)" if auto else f"Secret {name!r}"
        invalid = sorted(backend for backend in decl.backend_mappings if backend not in known_backends)
        if invalid:
            noun = "backend" if len(invalid) == 1 else "backends"
            g.fail(
                label,
                f"references unknown {noun}: {', '.join(invalid)}",
            )
            continue

        # Doctor is a pure inspection sweep, so its preview stays
        # optimistic about interactive availability
        # (``interactive_available=True``): it reports the secret's
        # configured capability, not whether this doctor run has a TTY.
        # Preflight prediction is the caller that gates on the run's mode
        # (issue #202).
        resolved_by = preview_resolution(decl, backends, interactive_available=True)
        if resolved_by is not None:
            g.ok(label, f"would resolve via {resolved_by}")
        else:
            # Readiness-aware (R9.6, in lockstep with the resolution skip): a
            # secret whose only attempting backend is not-ready is at-risk, and
            # the reason is the not-ready backend, not "no backend attempts it."
            skipped = [
                f"{b.name} ({b.readiness.reason})"
                for b in backends
                if b.would_attempt(decl) and not b.readiness.is_ready
            ]
            if skipped:
                g.warn(label, f"no ready backend would resolve it; not ready: {'; '.join(skipped)}")
            else:
                g.warn(label, "not available in any active backend")

    return g


def _check_database() -> HealthGroup:
    from agentworks.db import Database

    g = HealthGroup("Database")

    try:
        exists, current, latest = Database.check_schema()
        if not exists:
            g.ok("Database", "does not exist yet (will be created on first use)")
        elif current == latest:
            g.ok("Schema", f"up to date (version {current})")
            db = Database()
            _report_db_contents(g, db)
        elif current < latest:
            g.warn("Schema", f"at version {current}, latest is {latest}. Migrating...")
            db = Database()  # auto-migrates
            g.ok("Schema", f"migrated to version {latest}")
            _report_db_contents(g, db)
        else:
            g.fail("Schema", f"version {current} is newer than latest {latest} (downgrade?)")
    except Exception as e:
        g.fail("Database", str(e))

    return g


def _report_db_contents(g: HealthGroup, db: object) -> None:
    """Report DB contents and flag VMs in non-complete states."""
    from agentworks.db import Database, InitStatus
    from agentworks.ssh import LOG_DIR

    assert isinstance(db, Database)

    vms = db.list_vms()
    ws_count = len(db.list_workspaces())
    g.ok("Contents", f"{len(vms)} VMs, {ws_count} workspaces")

    def _log_hint(vm_name: str) -> str:
        if not LOG_DIR.exists():
            return ""
        logs = sorted(LOG_DIR.glob(f"{vm_name}-*.log"), reverse=True)
        return f" Log: {logs[0]}" if logs else ""

    for vm in vms:
        if vm.init_status == InitStatus.FAILED.value:
            g.warn(f"VM '{vm.name}'", f"failed state (only delete supported).{_log_hint(vm.name)}")
        elif vm.init_status == InitStatus.PARTIAL.value:
            g.warn(f"VM '{vm.name}'", f"initialized with warnings.{_log_hint(vm.name)}")
        elif vm.init_status not in (InitStatus.COMPLETE.value, InitStatus.PENDING.value):
            g.warn(f"VM '{vm.name}'", f"unexpected init status: {vm.init_status}")


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
