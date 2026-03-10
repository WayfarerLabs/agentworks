"""Health checks for the agentworks environment."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer


def run_doctor() -> None:
    """Run all health checks and report results."""
    ok_count = 0
    warn_count = 0
    fail_count = 0

    def ok(msg: str) -> None:
        nonlocal ok_count
        ok_count += 1
        typer.echo(f"  [ok]   {msg}")

    def warn(msg: str) -> None:
        nonlocal warn_count
        warn_count += 1
        typer.echo(f"  [warn] {msg}")

    def fail(msg: str) -> None:
        nonlocal fail_count
        fail_count += 1
        typer.echo(f"  [FAIL] {msg}")

    typer.echo("Checking environment...\n")

    # -- Python version ------------------------------------------------
    typer.echo("Python:")
    v = sys.version_info
    if v >= (3, 12):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        fail(f"Python {v.major}.{v.minor}.{v.micro} (3.12+ required)")

    # -- Required CLI tools -------------------------------------------
    typer.echo("\nRequired tools:")
    for tool in ("ssh", "scp", "tailscale"):
        if shutil.which(tool):
            ok(tool)
        else:
            fail(f"{tool} not found")
    if shutil.which("rsync"):
        ok("rsync")
    else:
        warn("rsync not found (needed for dotfiles sync)")

    # -- VM platforms --------------------------------------------------
    typer.echo("\nVM platforms:")

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
                    ok(f"VM host: {h.name} ({h.ssh_host}{os_info})")
            else:
                warn("No VM hosts configured (add with 'agentworks vm-host add')")
        else:
            warn("No VM hosts configured (database not yet created)")
    except Exception:
        warn("Could not check VM hosts")

    # Local platform tools
    for tool, label in [
        ("limactl", "Local Lima (limactl)"),
        ("wsl", "WSL2 (wsl)"),
    ]:
        if shutil.which(tool):
            ok(label)
        else:
            warn(f"{label} not found")

    # -- Tailscale connectivity ----------------------------------------
    typer.echo("\nTailscale:")
    ts = shutil.which("tailscale")
    if ts:
        try:
            result = subprocess.run(
                ["tailscale", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                if os.environ.get("TAILSCALE_AUTH_KEY"):
                    ok("Connected to tailnet (auth key env var set)")
                else:
                    ok("Connected to tailnet (will prompt for auth key during VM init)")
            else:
                fail("Not connected (run 'tailscale up')")
        except subprocess.TimeoutExpired:
            fail("'tailscale status' timed out")
    else:
        fail("tailscale not installed")

    # -- Config file ---------------------------------------------------
    typer.echo("\nConfiguration:")
    from agentworks.config import CONFIG_PATH, ConfigError

    config = None
    if not CONFIG_PATH.exists():
        fail(f"Config not found: {CONFIG_PATH}")
        fail("Run 'agentworks init' to create one")
    else:
        ok(f"Config exists: {CONFIG_PATH}")
        try:
            import warnings as _warnings

            from agentworks.config import load_config

            with _warnings.catch_warnings(record=True) as config_warnings:
                _warnings.simplefilter("always")
                config = load_config()
            for cw in config_warnings:
                warn(str(cw.message))
            ok("Config is valid")

            # SSH keys
            _check_ssh_key(config.user.ssh_public_key, "public", ok, warn, fail)
            _check_ssh_key(config.user.ssh_private_key, "private", ok, warn, fail)

            # Dotfiles
            if config.dotfiles.enabled:
                if config.dotfiles.source.exists():
                    ok(f"Dotfiles source: {config.dotfiles.source}")
                else:
                    warn(f"Dotfiles enabled but source missing: {config.dotfiles.source}")

            # Git credentials
            if config.git_credentials:
                typer.echo("\nGit credentials:")
                _check_git_credentials(config, ok, warn, fail)

        except ConfigError as e:
            fail(f"Config error: {e}")
        except SystemExit:
            fail("Config failed to load")

    # -- Database ------------------------------------------------------
    typer.echo("\nDatabase:")
    from agentworks.db import Database

    try:
        exists, current, latest = Database.check_schema()
        if not exists:
            ok("Database does not exist yet (will be created on first use)")
        elif current == latest:
            ok(f"Schema up to date (version {current})")
            db = Database()
            _report_db_contents(db, ok, warn)
        elif current < latest:
            warn(f"Schema at version {current}, latest is {latest}. Migrating...")
            db = Database()  # auto-migrates
            ok(f"Migrated to version {latest}")
            _report_db_contents(db, ok, warn)
        else:
            fail(f"Schema version {current} is newer than latest {latest} (downgrade?)")
    except Exception as e:
        fail(f"Database error: {e}")

    # -- Shell completions ---------------------------------------------
    typer.echo("\nShell completions:")
    _check_completions(ok, warn, fail)

    # -- Summary -------------------------------------------------------
    typer.echo(f"\nResults: {ok_count} ok, {warn_count} warnings, {fail_count} failures")
    if fail_count > 0:
        raise typer.Exit(1)


def _report_db_contents(
    db: object,
    ok: object,
    warn: object,
) -> None:
    """Report DB contents and flag VMs in non-complete states."""
    from agentworks.db import Database, InitStatus
    from agentworks.vms.init_log import find_init_logs

    assert isinstance(db, Database)
    assert callable(ok) and callable(warn)

    vms = db.list_vms()
    ws_count = len(db.list_workspaces())
    ok(f"{len(vms)} VMs, {ws_count} workspaces")

    for vm in vms:
        if vm.init_status == InitStatus.FAILED.value:
            logs = find_init_logs(vm.name)
            log_hint = f" Log: {logs[0]}" if logs else ""
            warn(f"VM '{vm.name}' is in 'failed' state (only delete supported).{log_hint}")
        elif vm.init_status == InitStatus.PARTIAL.value:
            logs = find_init_logs(vm.name)
            log_hint = f" Log: {logs[0]}" if logs else ""
            warn(f"VM '{vm.name}' initialized with warnings.{log_hint}")
        elif vm.init_status not in (InitStatus.COMPLETE.value, InitStatus.PENDING.value):
            warn(f"VM '{vm.name}' has unexpected init status: {vm.init_status}")


def _check_ssh_key(
    path: object,
    label: str,
    ok: object,
    warn: object,
    fail: object,
) -> None:
    """Check that an SSH key file exists and has correct permissions."""
    from pathlib import Path

    assert callable(ok) and callable(warn) and callable(fail)

    if not isinstance(path, Path):
        fail(f"SSH {label} key: invalid path")
        return

    if not path.exists():
        fail(f"SSH {label} key not found: {path}")
        return

    if not os.access(path, os.R_OK):
        fail(f"SSH {label} key not readable: {path}")
        return

    ok(f"SSH {label} key: {path}")

    # Check permissions on private key
    if label == "private":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            warn(f"SSH private key has broad permissions ({oct(mode)}), recommend 600")


def _check_git_credentials(
    config: object,
    ok: object,
    warn: object,
    fail: object,
) -> None:
    """Check git credential providers."""
    from agentworks.config import Config
    from agentworks.vms.initializer import resolve_git_credential_providers

    assert isinstance(config, Config)
    assert callable(ok) and callable(warn) and callable(fail)

    try:
        providers = resolve_git_credential_providers(config)
    except Exception as e:
        warn(f"Could not resolve git credential providers: {e}")
        return

    from agentworks.git_credentials.base import env_var_for_credential

    for name, provider in providers.items():
        try:
            if not provider.verify_auth():
                warn(f"{name}: auth check failed ({provider.auth_hint()})")
                continue
            if os.environ.get(env_var_for_credential(name)):
                ok(f"{name}: ready (token set via environment)")
            else:
                ok(f"{name}: ready (will prompt for token during VM init)")
        except Exception as e:
            warn(f"{name}: auth check error: {e}")


def _check_completions(
    ok: object,
    warn: object,
    fail: object,
) -> None:
    """Check shell completion file freshness."""
    assert callable(ok) and callable(warn) and callable(fail)

    from agentworks.cli import app
    from agentworks.completions.spec import build_spec, completion_version

    current_version = completion_version(build_spec(app))

    # Check all shells that have completions installed
    shells = _get_completion_paths()

    any_found = False
    for shell_name, candidate_paths in shells:
        for path in candidate_paths:
            if not path.exists():
                continue
            any_found = True
            installed_version = _read_completion_version(path)
            if installed_version == current_version:
                ok(f"{shell_name}: up to date")
            elif installed_version is None:
                warn(f"{shell_name}: no version stamp. See: agentworks completion {shell_name} --help")
            else:
                warn(f"{shell_name}: stale. See: agentworks completion {shell_name} --help")
    if not any_found:
        ok("No completions installed (install with: agentworks completion <shell> --install)")


def _get_completion_paths() -> list[tuple[str, list[Path]]]:
    """Return (shell_name, candidate_paths) for all shells.

    Only includes shells where completions are actually installed,
    so we don't warn about shells the user doesn't use.
    """
    home = Path.home()
    shells: list[tuple[str, list[Path]]] = []

    # Bash
    shells.append(("bash", [
        home / ".local" / "share" / "bash-completion" / "completions" / "agentworks",
    ]))

    # Zsh
    zsh_paths: list[Path] = [
        home / ".zfunc" / "_agentworks",
    ]
    zsh_custom = os.environ.get("ZSH_CUSTOM")
    if zsh_custom:
        zsh_paths.append(Path(zsh_custom) / "completions" / "_agentworks")
    omz_default = home / ".oh-my-zsh" / "custom" / "completions" / "_agentworks"
    if omz_default not in zsh_paths:
        zsh_paths.append(omz_default)
    shells.append(("zsh", zsh_paths))

    # PowerShell: derive path from $PROFILE (handles OneDrive redirection etc.)
    from agentworks.completions.install import _query_powershell_profile

    profile = _query_powershell_profile()
    if profile is not None:
        shells.append(("powershell", [
            profile.parent / "Completions" / "agentworks.ps1",
        ]))

    return shells


def _read_completion_version(path: Path) -> str | None:
    """Read the version stamp from a completion file."""
    try:
        with path.open() as f:
            for line in f:
                if line.startswith("# agentworks-completion-version:"):
                    return line.split(":", 1)[1].strip()
                # Only check the first few lines (skip blank lines)
                if not line.startswith("#") and line.strip():
                    break
    except OSError:
        pass
    return None
