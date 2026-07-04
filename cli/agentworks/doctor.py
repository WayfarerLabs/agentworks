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

    report.groups.append(_check_python())
    report.groups.append(_check_required_tools())
    report.groups.append(_check_vm_platforms())
    report.groups.append(_check_tailscale())

    config_group, config, registry = _check_config()
    report.groups.append(config_group)

    if config is not None and registry is not None:
        from agentworks.resources.access import kind_dict

        if kind_dict(registry, "git-credential"):
            report.groups.append(_check_git_credentials(registry))
        report.groups.append(_check_secrets(config, registry))

    report.groups.append(_check_database())

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
            # Phase 1c of the Resource Registry SDD routed the Tailscale
            # auth key through the framework; the legacy hard-coded
            # `AW_TAILSCALE_AUTH_KEY` env var no longer has a special
            # name. The auth key is now a `secret` Resource (default
            # name `tailscale-auth-key`); its resolution path is the
            # configured backend chain. `agw secret describe
            # tailscale-auth-key` (Phase 1e) is the right diagnostic
            # surface; this section just reports connectivity.
            g.ok(
                "Connected to tailnet",
                "auth key resolved via the secret framework at VM-init time",
            )
        else:
            g.fail("Not connected", "run 'tailscale up'")
    except subprocess.TimeoutExpired:
        g.fail("tailscale status", "timed out")
    return g


def _check_config() -> tuple[HealthGroup, Config | None, Registry | None]:
    """Returns (group, config_or_none, registry_or_none)."""
    from agentworks.config import CONFIG_PATH, ConfigError

    g = HealthGroup("Configuration")
    config = None

    if not CONFIG_PATH.exists():
        g.fail("Config file", f"not found: {CONFIG_PATH}. Run 'agw config init' to create one.")
        return g, None, None

    g.ok("Config file", str(CONFIG_PATH))

    try:
        from agentworks.config import load_config

        config = load_config(warn_issues=False)
    except ConfigError as e:
        g.fail("Config", str(e), hint=e.hint)
        return g, None, None
    except SystemExit:
        g.fail("Config", "failed to load")
        return g, None, None

    for issue in config.config_issues:
        g.warn("Config", issue)
    if not config.config_issues:
        g.ok("Config is valid")

    # SSH keys
    _check_ssh_key(g, config.operator.ssh_public_key, "public")
    _check_ssh_key(g, config.operator.ssh_private_key, "private")

    # Resource registry (framework validation: references, miss
    # policies, cycles). A failure here is a config problem, reported
    # like any other; the resource-dependent checks below are skipped.
    from agentworks.bootstrap import build_registry

    try:
        registry = build_registry(config)
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


def _check_git_credentials(registry: Registry) -> HealthGroup:
    """Check git credential providers."""
    from agentworks.resources.access import admin_template, kind_dict
    from agentworks.vms.initializer import resolve_git_credential_providers

    g = HealthGroup("Git credentials")

    # Collect all credential names from admin and agent templates
    all_cred_names: list[str] = list(admin_template(registry).git_credentials)
    for tmpl in kind_dict(registry, "agent-template").values():
        if tmpl.git_credentials is not None:
            for name in tmpl.git_credentials:
                if name not in all_cred_names:
                    all_cred_names.append(name)

    try:
        providers = resolve_git_credential_providers(registry, all_cred_names)
    except Exception as e:
        g.warn("Git credentials", f"could not resolve providers: {e}")
        return g

    # Phase 1d: tokens flow through the framework's backend chain
    # (env-var backend + prompt fallback by default; operator-typed
    # backends layered in via [secret_config].backends). Doctor stays
    # at the connectivity / authn-precondition layer; per-credential
    # token diagnostic detail lives in `agw secret describe
    # git-token-<name>` (Phase 1e).
    for provider in providers.values():
        label = provider.display_name
        try:
            if not provider.verify_auth():
                g.warn(label, f"auth check failed ({provider.auth_hint()})")
                continue
            g.ok(label, "ready (token resolved via the secret framework at VM-init time)")
        except Exception as e:
            g.warn(label, f"auth check error: {e}")

    return g


def _check_secrets(config: Config, registry: Registry) -> HealthGroup:
    """Check declared secrets per env-and-secrets SDD FRD R6.

    Emits exactly one row per declared secret:

    - OK: at least one active backend in the chain would resolve the
      secret at runtime.
    - WARN: no active backend would resolve it (config is valid but
      there's no path to a value -- e.g. env-var has no matching env
      var set and prompt is opted out).
    - FAIL: the secret's ``backend_mappings`` references an unknown
      backend kind (no ``[secret_backends.<kind>]`` section and not a
      built-in like env-var / prompt). Config error; nothing to resolve
      against. FAIL takes precedence over OK / WARN so the operator
      fixes the typo before we tell them about resolution.

    Backend-applicability detail (per-backend soft-skip reasons,
    inactive mappings) lives in ``agw secret list``; unused declarations
    surface in ``agw secret describe``'s ``Referenced by:`` section.
    Doctor stays one row per secret so the summary line stays scannable.
    """
    from agentworks.resources.access import kind_dict, secret_decls

    g = HealthGroup("Secrets")

    # Operator-declared rows only: doctor's Secrets group reports on
    # what the operator wrote (matching the pre-registry behavior);
    # auto-declared rows surface via `agw secret list` / `describe`.
    secrets = {
        name: decl
        for name, decl in secret_decls(registry).items()
        if getattr(decl.origin, "variant", None) == "operator-declared"
    }
    if not secrets:
        g.info("Declared secrets", "none")
        return g

    # The registry always carries the built-in env-var / prompt backend
    # rows, so this set covers built-ins and manifest declarations both.
    known_backends = set(kind_dict(registry, "secret-backend").keys())
    from agentworks.secrets.resolve import active_backends, preview_resolution

    backends = active_backends(config, registry)

    for name, decl in sorted(secrets.items()):
        invalid = sorted(
            backend
            for backend in decl.backend_mappings
            if backend not in known_backends
        )
        if invalid:
            noun = "backend" if len(invalid) == 1 else "backends"
            g.fail(
                f"Secret {name!r}",
                f"references unknown {noun}: {', '.join(invalid)}",
            )
            continue

        resolved_by = preview_resolution(decl, backends)
        if resolved_by is not None:
            g.ok(f"Secret {name!r}", f"would resolve via {resolved_by}")
        else:
            g.warn(f"Secret {name!r}", "not available in any active backend")

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
