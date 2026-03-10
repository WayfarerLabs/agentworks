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
    for tool in ("ssh", "scp", "rsync", "tailscale"):
        if shutil.which(tool):
            ok(tool)
        else:
            fail(f"{tool} not found")

    # -- Optional CLI tools -------------------------------------------
    typer.echo("\nOptional tools (at least one VM platform needed):")
    platforms_found = 0
    for tool, label in [
        ("limactl", "Lima (limactl)"),
        ("az", "Azure CLI (az)"),
        ("wsl", "WSL2 (wsl)"),
    ]:
        if shutil.which(tool):
            ok(label)
            platforms_found += 1
        else:
            warn(f"{label} not found")

    if platforms_found == 0:
        fail("No VM platform tools found (need limactl, az, or wsl)")

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
                ok("Connected to tailnet")
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

    # -- Environment variables -----------------------------------------
    typer.echo("\nEnvironment variables:")
    if os.environ.get("TAILSCALE_AUTH_KEY"):
        ok("TAILSCALE_AUTH_KEY is set")
    else:
        warn("TAILSCALE_AUTH_KEY not set (will prompt during VM create/start)")

    if config is not None and config.git_credentials:
        from agentworks.git_credentials.base import env_var_for_credential

        for cred_name in config.git_credentials:
            env_name = env_var_for_credential(cred_name)
            if os.environ.get(env_name):
                ok(f"{env_name} is set")
            else:
                warn(f"{env_name} not set (will prompt during VM init)")

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

    for name, provider in providers.items():
        try:
            if provider.verify_auth():
                ok(f"{name}: ready (will prompt for token during VM init)")
            else:
                warn(f"{name}: auth check failed ({provider.auth_hint()})")
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

    # Determine which shells to check based on platform
    if sys.platform == "win32":
        shells = _get_windows_completion_paths()
    else:
        shells = _get_unix_completion_paths()

    for shell_name, candidate_paths in shells:
        found = False
        for path in candidate_paths:
            if not path.exists():
                continue
            found = True
            installed_version = _read_completion_version(path)
            if installed_version == current_version:
                ok(f"{shell_name}: up to date ({path})")
            elif installed_version is None:
                warn(
                    f"{shell_name}: no version stamp ({path})."
                    f" Regenerate: agentworks completion {shell_name} > {path}"
                )
            else:
                warn(
                    f"{shell_name}: stale ({path})."
                    f" Regenerate: agentworks completion {shell_name} > {path}"
                )
        if not found:
            install_hint = _completion_install_hint(shell_name)
            warn(f"{shell_name}: completions not installed. Install with: {install_hint}")


def _get_unix_completion_paths() -> list[tuple[str, list[Path]]]:
    """Return (shell_name, candidate_paths) for Unix shells.

    Checks multiple common zsh completion directories in priority order:
    - ~/.zfunc (manual fpath setup)
    - Oh My Zsh custom completions ($ZSH_CUSTOM or default)
    - Homebrew zsh completions (macOS)
    """
    home = Path.home()
    zsh_paths: list[Path] = [
        home / ".zfunc" / "_agentworks",
    ]

    # Oh My Zsh: $ZSH_CUSTOM/completions/ or ~/.oh-my-zsh/custom/completions/
    zsh_custom = os.environ.get("ZSH_CUSTOM")
    if zsh_custom:
        zsh_paths.append(Path(zsh_custom) / "completions" / "_agentworks")
    omz_default = home / ".oh-my-zsh" / "custom" / "completions" / "_agentworks"
    if omz_default not in zsh_paths:
        zsh_paths.append(omz_default)

    return [
        ("zsh", zsh_paths),
    ]


def _get_windows_completion_paths() -> list[tuple[str, list[Path]]]:
    """Return (shell_name, candidate_paths) for Windows shells."""
    home = Path.home()
    return [
        (
            "powershell",
            [
                home / "Documents" / "PowerShell" / "Completions" / "agentworks.ps1",
                home / "Documents" / "WindowsPowerShell" / "Completions" / "agentworks.ps1",
            ],
        ),
    ]


def _completion_install_hint(shell_name: str) -> str:
    """Return a shell-specific install command hint."""
    if shell_name == "zsh":
        target = _best_zsh_completion_dir()
        return f"mkdir -p {target} && agentworks completion zsh > {target}/_agentworks"
    if shell_name == "powershell":
        return "agentworks completion powershell >> $PROFILE"
    return f"agentworks completion {shell_name}"


def _best_zsh_completion_dir() -> str:
    """Pick the best zsh completion directory for installation.

    Prefers Oh My Zsh custom completions if present, otherwise ~/.zfunc.
    """
    home = Path.home()

    # Oh My Zsh custom completions (auto-loaded, no fpath setup needed)
    zsh_custom = os.environ.get("ZSH_CUSTOM")
    if zsh_custom:
        return str(Path(zsh_custom) / "completions")
    omz_default = home / ".oh-my-zsh" / "custom"
    if omz_default.is_dir():
        return str(omz_default / "completions")

    return "~/.zfunc"


def _read_completion_version(path: Path) -> str | None:
    """Read the version stamp from a completion file."""
    try:
        with path.open() as f:
            for line in f:
                if line.startswith("# agentworks-completion-version:"):
                    return line.split(":", 1)[1].strip()
                # Only check the first few lines
                if not line.startswith("#"):
                    break
    except OSError:
        pass
    return None
