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
    from agentworks.env.entry import EnvEntry


# Marker phrase emitted by ``_parse_env_table`` in config.py for
# AGENTWORKS_* identity overrides. Used by ``_check_config`` (to
# suppress) and ``_check_env`` (to re-surface in the more specific
# Env group) so the warning appears once and only once per run.
_IDENTITY_ISSUE_MARKER = "sets agentworks-managed identity variable"


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


@dataclass
class HealthGroup:
    name: str
    checks: list[HealthCheck] = field(default_factory=list)

    def ok(self, name: str, message: str | None = None) -> None:
        self.checks.append(HealthCheck(name=name, status=Status.OK, message=message))

    def info(self, name: str, message: str | None = None) -> None:
        self.checks.append(HealthCheck(name=name, status=Status.INFO, message=message))

    def warn(self, name: str, message: str | None = None) -> None:
        self.checks.append(HealthCheck(name=name, status=Status.WARN, message=message))

    def fail(self, name: str, message: str | None = None) -> None:
        self.checks.append(HealthCheck(name=name, status=Status.FAIL, message=message))


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

    report.groups.append(_check_python())
    report.groups.append(_check_required_tools())
    report.groups.append(_check_vm_platforms())
    report.groups.append(_check_tailscale())

    config_group, config = _check_config()
    report.groups.append(config_group)

    if config is not None and config.git_credentials:
        report.groups.append(_check_git_credentials(config))

    if config is not None:
        report.groups.append(_check_secrets(config))
        report.groups.append(_check_env(config))

    report.groups.append(_check_database())

    # VM-side SSH probes (per ADR 0014): runs only when config + DB load
    # succeeded so this stays a no-op for fresh installs.
    if config is not None:
        report.groups.append(_check_vm_accept_env(config))

    if completion_version is not None:
        report.groups.append(_check_completions(completion_version))

    return report


# ---------------------------------------------------------------------------
# Individual check groups
# ---------------------------------------------------------------------------


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


def _check_vm_platforms() -> HealthGroup:
    g = HealthGroup("VM platforms")

    # VM hosts (remote Lima)
    try:
        from agentworks.db import Database

        db_exists, _, _ = Database.check_schema()
        if db_exists:
            _db = Database()
            hosts = _db.list_vm_hosts()
            if hosts:
                for h in hosts:
                    os_info = f", {h.os}" if h.os else ""
                    g.ok(f"VM host: {h.name}", f"{h.ssh_host}{os_info}")
            else:
                g.info("VM hosts", "none configured")
        else:
            g.info("VM hosts", "database not yet created")
    except Exception:
        g.warn("VM hosts", "could not check")

    # Local platform tools
    for tool, label in [
        ("limactl", "Local Lima (limactl)"),
        ("wsl", "WSL2 (wsl)"),
    ]:
        if shutil.which(tool):
            g.ok(label)
        else:
            g.info(label, "not available")
    return g


def _check_tailscale() -> HealthGroup:
    g = HealthGroup("Tailscale")
    ts = shutil.which("tailscale")
    if not ts:
        g.fail("tailscale", "not installed")
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
            from agentworks.env_compat import read_env_with_legacy

            if read_env_with_legacy("AW_TAILSCALE_AUTH_KEY", "TAILSCALE_AUTH_KEY"):
                g.ok("Connected to tailnet", "auth key env var set")
            else:
                g.ok("Connected to tailnet", "will prompt for auth key during VM init")
        else:
            g.fail("Not connected", "run 'tailscale up'")
    except subprocess.TimeoutExpired:
        g.fail("tailscale status", "timed out")
    return g


def _check_config() -> tuple[HealthGroup, Config | None]:
    """Returns (group, config_or_none)."""
    from agentworks.config import CONFIG_PATH, ConfigError

    g = HealthGroup("Configuration")
    config = None

    if not CONFIG_PATH.exists():
        g.fail("Config file", f"not found: {CONFIG_PATH}. Run 'agw config init' to create one.")
        return g, None

    g.ok("Config file", str(CONFIG_PATH))

    try:
        from agentworks.config import load_config

        config = load_config(warn_issues=False)
    except ConfigError as e:
        g.fail("Config", str(e))
        return g, None
    except SystemExit:
        g.fail("Config", "failed to load")
        return g, None

    # Identity-override issues are surfaced in the more specific Env group
    # (see ``_check_env``); skip them here to avoid double-reporting.
    config_issues = [
        issue for issue in config.config_issues
        if _IDENTITY_ISSUE_MARKER not in issue
    ]
    for issue in config_issues:
        g.warn("Config", issue)
    if not config_issues:
        g.ok("Config is valid")

    # SSH keys
    _check_ssh_key(g, config.operator.ssh_public_key, "public")
    _check_ssh_key(g, config.operator.ssh_private_key, "private")

    # Dotfiles
    if config.admin.dotfiles_source:
        from agentworks.sources import parse_source_ref

        ref = parse_source_ref(config.admin.dotfiles_source)
        if ref.kind == "git" or Path(ref.path).expanduser().exists():
            g.ok("Admin dotfiles", config.admin.dotfiles_source)
        else:
            g.warn("Admin dotfiles", f"source missing: {config.admin.dotfiles_source}")

    return g, config


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


def _check_git_credentials(config: Config) -> HealthGroup:
    """Check git credential providers."""
    from agentworks.vms.initializer import resolve_git_credential_providers

    g = HealthGroup("Git credentials")

    # Collect all credential names from admin and agent templates
    all_cred_names: list[str] = list(config.admin.git_credentials)
    for tmpl in config.agent_templates.values():
        if tmpl.git_credentials is not None:
            for name in tmpl.git_credentials:
                if name not in all_cred_names:
                    all_cred_names.append(name)

    try:
        providers = resolve_git_credential_providers(config, all_cred_names)
    except Exception as e:
        g.warn("Git credentials", f"could not resolve providers: {e}")
        return g

    from agentworks.env_compat import read_env_with_legacy
    from agentworks.git_credentials.base import env_var_for_credential, legacy_env_var_for_credential

    for name, provider in providers.items():
        label = provider.display_name
        try:
            if not provider.verify_auth():
                g.warn(label, f"auth check failed ({provider.auth_hint()})")
                continue
            if read_env_with_legacy(env_var_for_credential(name), legacy_env_var_for_credential(name)):
                g.ok(label, "ready (token set via environment)")
            else:
                g.ok(label, "ready (will prompt for token during VM init)")
        except Exception as e:
            g.warn(label, f"auth check error: {e}")

    return g


def _check_secrets(config: Config) -> HealthGroup:
    """Check declared secrets per env-and-secrets SDD FRD R6.

    For each declared secret, reports the first backend in the active
    chain that would attempt to resolve it (the "would-prompt preview"),
    flags soft-skips (backends that won't attempt this secret for lack of
    a default convention or an explicit mapping), and flags unused
    declarations (secrets nobody references).

    Also flags ``backend_mappings.<kind>`` keys whose kind is unknown
    (no [secret_backends.<kind>] section) as an error, and kinds that
    are declared but not active in ``[secret_config].backends`` as a
    warning. The active-resolver chain is config.secret_resolver.
    """
    g = HealthGroup("Secrets")

    if not config.secrets:
        g.info("Declared secrets", "none")
        return g

    # Build the set of secret names referenced by any env entry across all
    # five scopes so we can flag unused declarations.
    referenced: set[str] = set()

    def _collect(env: dict[str, EnvEntry] | None) -> None:
        if not env:
            return
        for entry in env.values():
            if entry.secret is not None:
                referenced.add(entry.secret)

    _collect(config.admin.env)
    for vt in config.vm_templates.values():
        _collect(vt.env)
    for wt in config.workspace_templates.values():
        _collect(wt.env)
    for at in config.agent_templates.values():
        _collect(at.env)
    for st in config.session_templates.values():
        _collect(st.env)

    # Set of backends declared in [secret_backends.*] (whether or not
    # active in [secret_config].backends).
    declared_backend_kinds = set(config.secret_backends.keys())
    active_backend_kinds = set(config.secret_config_data.backends)

    resolver = config.secret_resolver
    builtin_kinds = {"env_var", "prompt"}
    for name, decl in sorted(config.secrets.items()):
        # Would-prompt preview (FRD R6): probe non-prompt sources in
        # precedence order and report whether the secret would resolve
        # silently or fall through to an interactive prompt.
        outcome, kind = resolver.preview_resolution(decl)
        if outcome == "available":
            g.ok(
                f"secret {name!r}",
                f"available via {kind} (no prompt needed)",
            )
        elif outcome == "prompt":
            g.warn(
                f"secret {name!r}",
                "would prompt at command time (no non-interactive backend has a value)",
            )
        else:  # unreachable
            # Defensive: ``_build_secret_resolver`` raises at config-load
            # time for any secret that no active source would attempt, so
            # reaching this branch implies loader/resolver skew.
            g.fail(
                f"secret {name!r}",
                "no active backend would attempt to resolve it",
            )

        # Soft-skip findings.
        skipping = [s.kind for s in resolver.skipping_sources(decl)]
        if skipping:
            g.info(
                f"secret {name!r} soft-skipped by",
                ", ".join(skipping),
            )

        # Unused declaration warning.
        if name not in referenced:
            g.warn(
                f"secret {name!r}",
                "declared but not referenced by any env entry",
            )

        # backend_mappings sanity:
        # - kind not declared in [secret_backends.*] AND not a built-in
        #   (env_var / prompt) -> error (kind does not exist in this config).
        # - kind declared (or built-in) but not in [secret_config].backends
        #   -> warning (mapping has no effect; operator may be staging a
        #   disabled backend).
        for kind in decl.backend_mappings:
            if kind in declared_backend_kinds or kind in builtin_kinds:
                if kind not in active_backend_kinds:
                    g.warn(
                        f"secret {name!r} maps {kind}",
                        "backend not active in [secret_config].backends; "
                        "mapping has no effect in the current configuration",
                    )
            else:
                g.fail(
                    f"secret {name!r} maps {kind}",
                    f"no [secret_backends.{kind}] section declared",
                )

    return g


def _check_env(config: Config) -> HealthGroup:
    """Check env tables per env-and-secrets SDD FRD R6.

    Surfaces the config-load warnings already collected in
    ``config.config_issues`` (e.g. AGENTWORKS_* overrides) as env-group
    findings, and flags keys set at multiple FRD-R2 scopes as
    informational (the operator can refer to ``agw env show`` for the
    winning value).
    """
    g = HealthGroup("Env")

    # Track which FRD R2 scopes (vm / workspace / admin / agent / session)
    # set each key. Templates within a single scope kind are mutually
    # exclusive at runtime (only one VM template applies per VM), so we
    # collapse same-kind sources into a single scope label here to avoid
    # spurious "multiple scopes" reports.
    key_scopes: dict[str, set[str]] = {}

    def _record(env: dict[str, EnvEntry] | None, scope: str) -> None:
        if not env:
            return
        for key in env:
            key_scopes.setdefault(key, set()).add(scope)

    _record(config.admin.env, "admin")
    for vt in config.vm_templates.values():
        _record(vt.env, "vm")
    for wt in config.workspace_templates.values():
        _record(wt.env, "workspace")
    for at in config.agent_templates.values():
        _record(at.env, "agent")
    for st in config.session_templates.values():
        _record(st.env, "session")

    # Re-surface load-time config issues that flag AGENTWORKS_* identity
    # overrides. ``_parse_env_table`` records them on ``config_issues`` at
    # load time with the marker phrase "sets agentworks-managed identity
    # variable"; doctor reflects them as warn findings so they stay
    # visible across runs. The Configuration group filters them out (see
    # ``_check_config``) so the warning appears once, in the more specific
    # Env group.
    identity_issues = [
        issue for issue in config.config_issues
        if _IDENTITY_ISSUE_MARKER in issue
    ]
    for issue in identity_issues:
        g.warn("Identity override", issue)

    # Multi-scope key reports (informational; ``agw env show`` is the
    # authoritative tool for the winning value per context).
    multi_scope = [
        (key, sorted(scopes)) for key, scopes in sorted(key_scopes.items())
        if len(scopes) > 1
    ]
    if multi_scope:
        for key, scopes in multi_scope:
            g.info(
                f"env key {key!r}",
                f"set at multiple scopes ({', '.join(scopes)}); "
                "use `agw env show` for the effective value per context",
            )
    elif not identity_issues:
        g.ok("Env keys", f"{len(key_scopes)} declared, no cross-scope conflicts")

    return g


def _check_vm_accept_env(config: Config) -> HealthGroup:
    """Probe each provisioned VM for the AcceptEnv-wildcard sshd fragment
    deployed by Phase 4 (`/etc/ssh/sshd_config.d/50-agentworks-accept-env.conf`).

    Per ADR 0014: VMs predating the env-and-secrets SDD silently drop
    SetEnv'd env at sshd until they're reinit'd. This check surfaces
    them so the operator can plan the reinit explicitly.

    Skips VMs that aren't fully provisioned or have no tailscale_host
    (probing them would fail for unrelated reasons). Each probe runs
    with a short timeout; a small thread pool keeps wall-clock bounded
    when the operator has many VMs.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from agentworks.db import Database, ProvisioningStatus
    from agentworks.ssh import SSHError, admin_exec_target

    g = HealthGroup("VM env support")

    try:
        exists, current, latest = Database.check_schema()
        if not exists or current != latest:
            # Doctor's database check group will surface this; nothing
            # actionable here without a current schema.
            return g
        db = Database()
        vms = [
            v for v in db.list_vms()
            if v.provisioning_status == ProvisioningStatus.COMPLETE.value
            and v.tailscale_host is not None
        ]
    except Exception as e:
        g.warn("VM probe", f"could not list VMs: {e}")
        return g

    if not vms:
        g.info("VM probe", "no provisioned VMs to check")
        return g

    probe_cmd = "test -f /etc/ssh/sshd_config.d/50-agentworks-accept-env.conf"

    # SQLite connections are thread-bound; build the per-VM ExecTarget
    # in the main thread, then run the SSH probes concurrently against
    # already-resolved targets. Short timeout: doctor is interactive and
    # the probe is a trivial `test -f`, so a stopped or unreachable VM
    # should fail fast rather than block the whole sweep.
    _probe_timeout = 5
    targets: list[tuple[str, object]] = []
    for vm in vms:
        try:
            targets.append(
                (vm.name, admin_exec_target(vm, config, default_timeout=_probe_timeout))
            )
        except Exception as e:  # noqa: BLE001 - defensive: unreachable target degrades cleanly
            targets.append((vm.name, e))

    def _probe(name: str, target_or_err: object) -> tuple[str, str, str | None]:
        if isinstance(target_or_err, Exception):
            return (name, "unreachable", str(target_or_err))
        try:
            result = target_or_err.run(probe_cmd, check=False, timeout=_probe_timeout)  # type: ignore[attr-defined]
        except SSHError as e:
            return (name, "unreachable", str(e))
        except Exception as e:  # noqa: BLE001 - defensive: any transport error degrades to "unreachable"
            return (name, "unreachable", str(e))
        if result.ok:
            return (name, "ok", None)
        return (name, "missing", None)

    results: list[tuple[str, str, str | None]] = []
    max_workers = min(8, len(targets))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_probe, n, t): n for n, t in targets}
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: r[0])
    for name, status, detail in results:
        if status == "ok":
            g.ok(f"VM {name!r}", "AcceptEnv wildcard present")
        elif status == "missing":
            g.warn(
                f"VM {name!r}",
                "missing AcceptEnv wildcard; SSH SetEnv from agentworks "
                "is silently dropped. Run `agw vm reinit` to deploy the "
                "sshd fragment.",
            )
        else:  # unreachable
            g.info(
                f"VM {name!r}",
                f"could not probe (SSH unreachable): {detail or 'no detail'}",
            )

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
        for path in candidate_paths:
            if not path.exists():
                continue
            any_found = True
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


def _get_completion_paths() -> list[tuple[str, list[Path]]]:
    """Return (shell_name, candidate_paths) for all shells."""
    home = Path.home()
    shells: list[tuple[str, list[Path]]] = []

    # Bash
    shells.append((
        "bash",
        [home / ".local" / "share" / "bash-completion" / "completions" / "agentworks"],
    ))

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
        shells.append((
            "powershell",
            [profile.parent / "Completions" / "agentworks.ps1"],
        ))

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
