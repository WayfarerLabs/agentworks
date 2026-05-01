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

    @property
    def ok_count(self) -> int:
        return sum(1 for g in self.groups for c in g.checks if c.status == Status.OK)

    @property
    def info_count(self) -> int:
        return sum(1 for g in self.groups for c in g.checks if c.status == Status.INFO)

    @property
    def warn_count(self) -> int:
        return sum(1 for g in self.groups for c in g.checks if c.status == Status.WARN)

    @property
    def fail_count(self) -> int:
        return sum(1 for g in self.groups for c in g.checks if c.status == Status.FAIL)

    @property
    def has_failures(self) -> bool:
        return self.fail_count > 0


def run_checks() -> HealthReport:
    """Run all health checks and return structured results."""
    report = HealthReport()

    report.groups.append(_check_python())
    report.groups.append(_check_required_tools())
    report.groups.append(_check_vm_platforms())
    report.groups.append(_check_tailscale())

    config_group, config = _check_config()
    report.groups.append(config_group)

    if config is not None and config.git_credentials:
        report.groups.append(_check_git_credentials(config))

    report.groups.append(_check_database())

    # Completions check needs the CLI app to compute the spec version.
    # Import here (not in _check_completions) to keep doctor decoupled.
    try:
        from agentworks.cli import app
        from agentworks.completions.spec import build_spec, completion_version

        report.groups.append(_check_completions(completion_version(build_spec(app))))
    except Exception:
        g = HealthGroup("Shell completions")
        g.warn("Completions", "could not check (CLI import failed)")
        report.groups.append(g)

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
            if os.environ.get("TAILSCALE_AUTH_KEY"):
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
        g.fail("Config file", f"not found: {CONFIG_PATH}")
        g.fail("Config file", "run 'agentworks config init' to create one")
        return g, None

    g.ok("Config file", str(CONFIG_PATH))

    try:
        import warnings as _warnings

        from agentworks.config import load_config

        with _warnings.catch_warnings(record=True) as config_warnings:
            _warnings.simplefilter("always")
            config = load_config()
        for cw in config_warnings:
            g.warn("Config", str(cw.message))
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

    except ConfigError as e:
        g.fail("Config", str(e))
    except SystemExit:
        g.fail("Config", "failed to load")

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

    from agentworks.git_credentials.base import env_var_for_credential

    for name, provider in providers.items():
        label = provider.display_name
        try:
            if not provider.verify_auth():
                g.warn(label, f"auth check failed ({provider.auth_hint()})")
                continue
            if os.environ.get(env_var_for_credential(name)):
                g.ok(label, "ready (token set via environment)")
            else:
                g.ok(label, "ready (will prompt for token during VM init)")
        except Exception as e:
            g.warn(label, f"auth check error: {e}")

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
                g.warn(shell_name, f"no version stamp. See: agentworks completion {shell_name} --help")
            else:
                g.warn(shell_name, f"stale. See: agentworks completion {shell_name} --help")
    if not any_found:
        g.ok("Completions", "none installed (install with: agentworks completion <shell> --install)")

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
