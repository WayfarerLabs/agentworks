"""VM lifecycle: provisioning (one-time) and initialization (repeatable).

Two phases:
  A. Provisioning (over provisioning transport): bootstrap, SSH key, Tailscale join.
     One-time, platform-specific, pass/fail. Tracked via provisioning_status.
  B. Initialization (over Tailscale SSH): packages, install commands, git credentials,
     dotfiles. Repeatable via `vm reinit`. Tracked via init_status.

Phase A steps are fatal -- if they fail, the VM is unreachable and useless.
Phase B steps are non-fatal -- failures produce warnings and a 'partial' status.
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from agentworks.db import InitStatus, ProvisioningStatus
from agentworks.ssh import ExecTarget, SSHError, SSHLogger, SSHTarget
from agentworks.vms.cloud_init import INIT_SYSTEM_PACKAGES, PROVISIONING_PACKAGES

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.catalog import AptSourceEntry, SystemInstallCommandEntry, UserInstallCommandEntry
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.git_credentials.base import GitCredentialProvider
    from agentworks.ssh import SSHResult


def _run_logged(
    target: ExecTarget,
    command: str,
    logger: SSHLogger,
    *,
    as_root: bool = False,
    check: bool = True,
    timeout: int | None = None,
) -> SSHResult:
    """Run a command on the target and log the command + full output."""
    logger.output(f"$ {command}")
    result = (
        target.run_as_root(command, check=check, timeout=timeout)
        if as_root
        else target.run(
            command,
            check=check,
            timeout=timeout,
        )
    )
    if result.stdout:
        logger.output(result.stdout)
    if result.stderr:
        logger.output(result.stderr)
    return result


AGENTWORKS_PROFILE = ".agentworks-profile.sh"
AGENTWORKS_RC = ".agentworks-rc.sh"


def _write_agentworks_profile(
    target: ExecTarget,
    path_additions: list[str],
    logger: SSHLogger,
) -> None:
    """Write the agentworks-managed login profile fragment.

    Writes $HOME/.agentworks-profile.sh with PATH exports and env vars.
    Sourced from ~/.profile (bash/sh) and ~/.zprofile (zsh) -- runs once
    per login shell, inherited by child processes.
    Always written (even if empty) so that reinit can clear previously set paths.
    """
    # Deduplicate paths while preserving order
    seen: set[str] = set()
    unique_paths: list[str] = []
    for p in path_additions:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    logger.step("Shell profile")
    typer.echo(f"  Writing agentworks profile ({len(unique_paths)} PATH entries)...")

    try:
        lines = ["# Managed by agentworks -- do not edit"]
        for p in unique_paths:
            expanded = p.replace("~", "$HOME", 1) if p.startswith("~") else p
            lines.append(f'export PATH="{expanded}:$PATH"')
        target.write_file(f"~/{AGENTWORKS_PROFILE}", "\n".join(lines) + "\n")

        # Source from ~/.profile (bash/sh) and ~/.zprofile (zsh)
        source_line = f". $HOME/{AGENTWORKS_PROFILE}"
        for rc in ("$HOME/.profile", "$HOME/.zprofile"):
            _run_logged(
                target,
                f"grep -q {AGENTWORKS_PROFILE} {rc} 2>/dev/null || printf '%s\\n' '{source_line}' >> {rc}",
                logger,
            )
    except SSHError as e:
        msg = f"shell profile write failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)


def _write_agentworks_rc(
    target: ExecTarget,
    shell_snippets: list[str],
    logger: SSHLogger,
) -> None:
    """Write the agentworks-managed rc fragment for interactive shells.

    Writes $HOME/.agentworks-rc.sh with shell hooks (e.g., mise activate).
    Sourced from ~/.bashrc and ~/.zshrc -- runs per interactive shell instance.
    Always written (even if empty) so that reinit can clear previously set hooks.
    """
    logger.step("Shell rc")
    typer.echo("  Writing agentworks rc...")

    try:
        lines = ["# Managed by agentworks -- do not edit"]
        lines.extend(shell_snippets)
        target.write_file(f"~/{AGENTWORKS_RC}", "\n".join(lines) + "\n")

        # Source from ~/.bashrc and ~/.zshrc
        source_line = f". $HOME/{AGENTWORKS_RC}"
        for rc in ("$HOME/.bashrc", "$HOME/.zshrc"):
            _run_logged(
                target,
                f"grep -q {AGENTWORKS_RC} {rc} 2>/dev/null || printf '%s\\n' '{source_line}' >> {rc}",
                logger,
            )
    except SSHError as e:
        msg = f"shell rc write failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)


# -- Mise installation ---------------------------------------------------------

MISE_GPG_KEY_URL = "https://mise.jdx.dev/gpg-key.pub"
MISE_GPG_KEY_PATH = "/etc/apt/keyrings/mise-archive-keyring.asc"
MISE_SOURCE_LINE = f"deb [signed-by={MISE_GPG_KEY_PATH}] https://mise.jdx.dev/deb stable main"
MISE_SOURCE_FILE = "/etc/apt/sources.list.d/mise.list"


MISE_ACTIVATE_LINES = (
    "# agentworks-mise-activate\n"
    'if [ -n "$ZSH_VERSION" ]; then\n'
    '  eval "$(mise activate zsh)"\n'
    'elif [ -n "$BASH_VERSION" ]; then\n'
    '  eval "$(mise activate bash)"\n'
    "else\n"
    '  echo "agentworks: mise activate skipped (unsupported shell)" >&2\n'
    "fi"
)


def _mise_shims_path(home: str) -> list[str]:
    """Return PATH additions for mise shims (for non-interactive contexts)."""
    return [f"{home}/.local/share/mise/shims"]


def _write_mise_config(
    target: ExecTarget,
    packages: list[str],
    install_before: str,
    home: str,
    logger: SSHLogger,
) -> None:
    """Write ~/.config/mise/config.toml from mise_packages list.

    Packages are name@version strings (e.g., "jq@1.8.1").
    """
    if not packages:
        return

    logger.step("Mise config")
    typer.echo(f"  Writing mise config with {len(packages)} package(s)...")

    settings_lines = ["[settings]", f'install_before = "{install_before}"', ""]
    tools_lines = ["[tools]"]

    for pkg in packages:
        if "@" in pkg:
            name, version = pkg.rsplit("@", 1)
            tools_lines.append(f'"{name}" = "{version}"')
        else:
            tools_lines.append(f'"{pkg}" = "latest"')

    mise_config = "\n".join(settings_lines + tools_lines) + "\n"

    try:
        mise_config_dir = f"{home}/.config/mise"
        _run_logged(target, f"mkdir -p {mise_config_dir}", logger)
        target.write_file(f"{mise_config_dir}/config.toml", mise_config)
    except SSHError as e:
        msg = f"mise config write failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)


def _fetch_mise_lockfile(
    target: ExecTarget,
    lockfile_source: str,
    home: str,
    logger: SSHLogger,
) -> None:
    """Fetch a mise lockfile from a source reference to ~/.config/mise/mise.lock."""
    from agentworks.sources import SourceRefError, fetch_file, parse_source_ref

    logger.step("Mise lockfile")
    typer.echo(f"  Fetching mise lockfile from {lockfile_source}...")

    try:
        ref = parse_source_ref(lockfile_source, default_filename="mise.lock")
        dest = f"{home}/.config/mise/mise.lock"
        _run_logged(target, f"mkdir -p {home}/.config/mise", logger)
        fetch_file(ref, target, dest, logger=logger)
    except SourceRefError as e:
        msg = f"mise lockfile fetch failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)


def _parse_mise_failures(error: SSHError) -> list[str]:
    """Extract failed tool names from mise stderr output.

    Parses lines like:
      mise ERROR Failed to install aqua:npryce/adr-tools@3.0.0: reason here
    The tool name can contain colons (backend:path@version), so we split
    on ": " (colon-space) to separate tool from reason.
    """
    failures: list[str] = []
    error_str = str(error)
    for line in error_str.splitlines():
        if "Failed to install" in line:
            part = line.split("Failed to install", 1)[1].strip()
            tool = part.split(": ", 1)[0].strip()
            if tool and tool not in failures:
                failures.append(tool)
    return failures


def _run_mise_install(
    target: ExecTarget,
    shell: str,
    home: str,
    allow_unlocked: bool,
    logger: SSHLogger,
    *,
    prune: bool = True,
) -> None:
    """Run mise install, handling locked/unlocked modes.

    If a lockfile is present, tries --locked first. If that fails due to
    unlocked packages and allow_unlocked is true, retries without --locked.
    """
    logger.step("Mise install")

    # Check if a lockfile is present
    lockfile_path = f"{home}/.config/mise/mise.lock"
    has_lockfile = False
    try:
        check = target.run(f"test -f {lockfile_path}", check=False)
        has_lockfile = check.ok
    except SSHError:
        pass

    installed = False

    if has_lockfile:
        typer.echo("  Running mise install (locked)...")
        try:
            _run_logged(
                target,
                f"{shell} -lc 'mise install -y --locked'",
                logger,
                timeout=300,
            )
            typer.echo("  Mise packages installed (locked)")
            installed = True
        except SSHError as e:
            logger.warning(f"mise install --locked failed: {e}")
            failures = _parse_mise_failures(e)
            for tool in failures:
                typer.echo(f"  Locked install failed, not in lockfile: {tool}", err=True)
            if not failures:
                typer.echo("  Warning: mise install --locked failed (see vm logs)", err=True)
            if not allow_unlocked:
                typer.echo("  Hint: set mise_allow_unlocked = true to install unlocked packages", err=True)
                return
            typer.echo("  Retrying unlocked...", err=True)

    if not installed:
        typer.echo("  Running mise install...")
        try:
            _run_logged(
                target,
                f"{shell} -lc 'mise install -y'",
                logger,
                timeout=300,
            )
            typer.echo("  Mise packages installed")
            installed = True
        except SSHError as e:
            logger.warning(f"mise install failed: {e}")
            failures = _parse_mise_failures(e)
            for tool in failures:
                typer.echo(f"  Failed: {tool}", err=True)
            if not failures:
                typer.echo("  Warning: mise install failed (see vm logs)", err=True)

    # Prune stale tool versions not in the current config
    if installed and prune:
        import contextlib

        with contextlib.suppress(SSHError):
            _run_logged(target, f"{shell} -lc 'mise prune -y'", logger, timeout=60)


# -- SSH authorized keys ------------------------------------------------------

AUTHORIZED_KEYS_HEADER = """\
# Managed by agentworks -- manual edits will be overwritten on reinit.
# To add keys, use user.extra_ssh_public_keys in your agentworks config.
"""


def _reconcile_authorized_keys(
    target: ExecTarget,
    config: Config,
    home: str,
    logger: SSHLogger,
) -> None:
    """Reconcile ~/.ssh/authorized_keys with the configured key set.

    Writes the primary ssh_public_key plus any extra_ssh_public_keys from
    config. This is a full overwrite so that removed keys are cleaned up
    on reinit.
    """
    logger.step("SSH authorized keys")

    keys: list[str] = [config.user.ssh_public_key.read_text().strip()]
    for path in config.user.extra_ssh_public_keys:
        keys.append(path.read_text().strip())

    extra_count = len(keys) - 1
    label = f"1 primary + {extra_count} extra" if extra_count else "1 primary"
    typer.echo(f"  Reconciling authorized_keys ({label})...")

    content = AUTHORIZED_KEYS_HEADER + "\n".join(keys) + "\n"
    try:
        target.write_file(f"{home}/.ssh/authorized_keys", content, mode="600")
    except SSHError as e:
        msg = f"authorized_keys reconciliation failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)


def _configure_apt_sources(
    target: ExecTarget,
    config: Config,
    catalog: object,
    logger: SSHLogger,
) -> None:
    """Configure apt sources required by selected apt_packages. Idempotent."""
    from agentworks.catalog import ResolvedCatalog

    assert isinstance(catalog, ResolvedCatalog)

    # Collect all apt sources needed by selected apt_packages
    required_sources: dict[str, AptSourceEntry] = {}
    for pkg_name in config.vm.apt_packages:
        pkg = catalog.apt_packages.get(pkg_name)
        if pkg is None:
            continue
        for src_name in pkg.apt_sources:
            if src_name not in required_sources:
                src = catalog.apt_sources.get(src_name)
                if src is not None:
                    required_sources[src_name] = src

    if not required_sources:
        return

    logger.step("Apt sources")

    # Detect architecture
    arch_result = target.run("dpkg --print-architecture", check=False)
    arch = arch_result.stdout.strip() if arch_result.returncode == 0 else "amd64"

    newly_configured = False
    for name, src in required_sources.items():
        # Check if GPG key already exists
        key_exists = target.run(f"test -f {shlex.quote(src.key_path)}", check=False).returncode == 0

        if not key_exists:
            typer.echo(f"  Configuring apt source '{name}'...")
            try:
                # Ensure parent directory for key_path exists
                from pathlib import PurePosixPath

                key_dir = str(PurePosixPath(src.key_path).parent)
                _run_logged(target, f"install -m 0755 -d {shlex.quote(key_dir)}", logger, as_root=True)

                # Download GPG key
                if src.key_dearmor:
                    # Wrap in sh -c so sudo applies to the entire pipeline,
                    # not just the curl on the left side of the pipe.
                    inner = f"curl -fsSL {shlex.quote(src.key_url)} | gpg --dearmor -o {shlex.quote(src.key_path)}"
                    _run_logged(
                        target,
                        f"sh -c {shlex.quote(inner)}",
                        logger,
                        as_root=True,
                        timeout=60,
                    )
                else:
                    _run_logged(
                        target,
                        f"curl -fsSL {shlex.quote(src.key_url)} -o {shlex.quote(src.key_path)}",
                        logger,
                        as_root=True,
                        timeout=60,
                    )
                _run_logged(target, f"chmod a+r {shlex.quote(src.key_path)}", logger, as_root=True)
            except SSHError as exc:
                msg = f"apt source '{name}' failed: {exc}"
                logger.warning(msg)
                typer.echo(f"  Warning: {msg}", err=True)
                continue

        # Always ensure the source list file has the correct content,
        # even when the key already existed (the source URL may have changed).
        resolved_source = src.source.replace("{arch}", arch)
        source_path = f"/etc/apt/sources.list.d/{src.source_file}"
        expected = resolved_source + "\n"
        current = target.run(f"cat {shlex.quote(source_path)}", check=False)
        if current.returncode == 0 and current.stdout == expected:
            if key_exists:
                typer.echo(f"  Apt source '{name}': already configured, skipping")
                logger.output(f"apt source {name}: key and source list up to date, skipping")
            continue

        if key_exists:
            typer.echo(f"  Apt source '{name}': updating source list...")
            logger.output(f"apt source {name}: key exists but source list needs update")

        try:
            _run_logged(
                target,
                f"bash -c {shlex.quote(f'printf "%s\\n" {shlex.quote(resolved_source)} > {source_path}')}",
                logger,
                as_root=True,
            )
            newly_configured = True
        except SSHError as e:
            msg = f"apt source '{name}' failed: {e}"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)

    if newly_configured:
        typer.echo("  Running apt-get update...")
        try:
            _run_logged(target, "apt-get update -qq", logger, as_root=True, timeout=120)
        except SSHError as e:
            msg = f"apt-get update failed after adding sources: {e}"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)


def _install_system_packages(
    target: ExecTarget,
    logger: SSHLogger,
) -> None:
    """Install system repos and packages. Always runs on every init/reinit."""
    logger.step("System packages")

    # Add mise apt source
    try:
        _run_logged(
            target,
            f"curl -fsSL {MISE_GPG_KEY_URL} -o {MISE_GPG_KEY_PATH}",
            logger,
            as_root=True,
            timeout=30,
        )
        inner = f"printf '%s\\n' '{MISE_SOURCE_LINE}' > {MISE_SOURCE_FILE}"
        _run_logged(target, f"sh -c {shlex.quote(inner)}", logger, as_root=True)
    except SSHError as e:
        msg = f"mise apt source setup failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)

    typer.echo("  Running apt-get update...")
    try:
        _run_logged(target, "apt-get update -qq", logger, as_root=True, timeout=120)
    except SSHError as e:
        msg = f"apt-get update failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)

    typer.echo(f"  Installing {len(INIT_SYSTEM_PACKAGES)} system packages...")
    apt_str = " ".join(shlex.quote(p) for p in INIT_SYSTEM_PACKAGES)
    try:
        _run_logged(
            target,
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o Dpkg::Options::=--force-confnew {apt_str}",
            logger,
            as_root=True,
            timeout=300,
        )
    except SSHError as e:
        msg = f"system packages failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)


def _install_apt_packages(
    target: ExecTarget,
    config: Config,
    catalog: object,
    logger: SSHLogger,
) -> None:
    """Install apt packages from both direct list and catalog entries."""
    from agentworks.catalog import ResolvedCatalog

    assert isinstance(catalog, ResolvedCatalog)

    # Collect all apt packages: direct list + catalog entries
    all_apt: list[str] = list(config.vm.apt)
    for pkg_name in config.vm.apt_packages:
        pkg = catalog.apt_packages.get(pkg_name)
        if pkg is not None:
            all_apt.extend(pkg.apt)

    if not all_apt:
        return

    logger.step("Apt packages")
    typer.echo(f"  Installing {len(all_apt)} apt packages...")
    apt_str = " ".join(shlex.quote(p) for p in all_apt)
    try:
        _run_logged(
            target,
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o Dpkg::Options::=--force-confnew {apt_str}",
            logger,
            as_root=True,
            timeout=300,
        )
    except SSHError as e:
        msg = f"apt packages failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)


def _build_test_command(
    entry: SystemInstallCommandEntry | UserInstallCommandEntry,
    shell: str,
    home: str,
) -> str | None:
    """Build a shell command to check if an install command's tool is present.

    test_exec uses a login shell (-l) with interactive flag (-i) to ensure
    all profile/rc files are sourced, matching a real login session.
    """
    if entry.test_exec:
        return f"{shell} -lic {shlex.quote(f'command -v {shlex.quote(entry.test_exec)}')} > /dev/null 2>&1"
    if entry.test_file:
        path = entry.test_file.replace("~", home, 1) if entry.test_file.startswith("~") else entry.test_file
        return f"test -f {shlex.quote(path)}"
    if entry.test_dir:
        path = entry.test_dir.replace("~", home, 1) if entry.test_dir.startswith("~") else entry.test_dir
        return f"test -d {shlex.quote(path)}"
    return None


def _run_catalog_commands(
    target: ExecTarget,
    command_names: list[str],
    entries: Mapping[str, SystemInstallCommandEntry | UserInstallCommandEntry],
    shell: str,
    home: str,
    logger: SSHLogger,
    *,
    label: str = "Install command",
) -> list[str]:
    """Run install commands from a catalog entry dict. Returns PATH additions."""
    if not command_names:
        return []

    path_additions: list[str] = []
    total = len(command_names)

    for i, name in enumerate(command_names, 1):
        entry = entries.get(name)
        if entry is None:
            msg = f"{label.lower()} '{name}' not found in catalog"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)
            continue
        logger.step(f"{label} {i}/{total}: {name}")

        # Skip if already installed (short timeout -- this should be instant)
        test_cmd = _build_test_command(entry, shell, home)
        if test_cmd:
            try:
                check = target.run(test_cmd, check=False, timeout=10)
                if check.returncode == 0:
                    typer.echo(f"  {label} {i}/{total} ({name}): already installed, skipping")
                    logger.output(f"{name}: already installed ({test_cmd}), skipping")
                    path_additions.extend(entry.path)
                    continue
            except SSHError as e:
                # Timeout or connection issue -- assume not installed, proceed
                logger.output(f"{name}: install check failed ({e}), assuming not installed")

        truncated = entry.command[:60]
        typer.echo(f"  {label} {i}/{total} ({name}): {truncated}...")
        try:
            _run_logged(target, f"{shlex.quote(shell)} -lc {shlex.quote(entry.command)}", logger, timeout=120)
        except SSHError as e:
            msg = f"{label.lower()} '{name}' failed: {truncated}... ({e})"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)
        path_additions.extend(entry.path)

    return path_additions


def verify_tailscale_available() -> None:
    """Pre-flight: verify the local machine is on Tailscale."""
    try:
        result = subprocess.run(
            ["tailscale", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
        )
    except FileNotFoundError:
        typer.echo("Error: 'tailscale' command not found. Install Tailscale on this machine.", err=True)
        raise typer.Exit(1) from None
    except subprocess.TimeoutExpired:
        typer.echo("Error: 'tailscale status' timed out. Is Tailscale running?", err=True)
        raise typer.Exit(1) from None

    if result.returncode != 0:
        typer.echo(
            "Error: This machine is not connected to Tailscale. "
            "VM initialization requires Tailscale to switch from the provisioning "
            "transport to direct SSH. Run 'tailscale up' first.",
            err=True,
        )
        raise typer.Exit(1)


def resolve_git_credential_providers(
    config: Config,
    names: list[str],
) -> dict[str, GitCredentialProvider]:
    """Resolve git credential provider instances from config.

    Names are the credential names to resolve (from admin.config.git_credentials
    or agent.config.git_credentials).
    """
    from agentworks.git_credentials.azdo import AzDOCredentialProvider
    from agentworks.git_credentials.github import GitHubCredentialProvider

    providers: dict[str, GitCredentialProvider] = {}
    if not names:
        return providers
    for name in names:
        cred_config = config.git_credentials.get(name)
        if cred_config is None:
            typer.echo(f"Error: git credential '{name}' not found in config", err=True)
            raise typer.Exit(1)
        desc = cred_config.description
        if cred_config.type == "azdo":
            assert cred_config.org is not None
            providers[name] = AzDOCredentialProvider(config_name=name, org=cred_config.org, description=desc)
        elif cred_config.type == "github":
            providers[name] = GitHubCredentialProvider(config_name=name, description=desc)
    return providers


def verify_git_credential_auth(providers: dict[str, GitCredentialProvider]) -> None:
    """Pre-flight: verify auth for all selected git credential providers."""
    for name, provider in providers.items():
        if not provider.verify_auth():
            typer.echo(f"Error: Authentication check failed for '{name}'. {provider.auth_hint()}", err=True)
            raise typer.Exit(1)
    if providers:
        labels = [p.display_name for p in providers.values()]
        typer.echo(f"Git credentials configured: {', '.join(labels)}")


def rejoin_tailscale(
    db: Database,
    vm_name: str,
    exec_target: ExecTarget,
    *,
    is_wsl2: bool = False,
) -> str:
    """Re-join Tailscale on a VM that lost its node (e.g. ephemeral key).

    Installs Tailscale if needed, prompts for an auth key, joins the tailnet,
    and updates the DB with the new Tailscale IP.

    Returns the new Tailscale IP.
    """
    typer.echo("Tailscale node not reachable. Re-joining tailnet...")

    # Ensure Tailscale is installed (idempotent)
    exec_target.run_as_root(
        "bash -c 'command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh'",
        check=False,
    )

    return _join_tailscale(db, vm_name, exec_target, is_wsl2=is_wsl2)


def _join_tailscale(
    db: Database,
    vm_name: str,
    exec_target: ExecTarget,
    *,
    is_wsl2: bool = False,
    logger: SSHLogger | None = None,
    tailscale_auth_key: str | None = None,
) -> str:
    """Join Tailscale, update DB. Returns the Tailscale IP."""
    import os

    ts_auth_key = tailscale_auth_key or os.environ.get("TAILSCALE_AUTH_KEY")
    if not ts_auth_key:
        from agentworks.prompt import prompt_secret

        ts_auth_key = prompt_secret(
            "  Tailscale auth key",
            hint="Generate a key at https://login.tailscale.com/admin/settings/keys",
        )
    ts_cmd = f"tailscale up --auth-key {ts_auth_key}"
    if is_wsl2:
        ts_cmd += " --userspace-networking"

    if logger:
        _run_logged(exec_target, ts_cmd, logger, as_root=True)
        result = _run_logged(exec_target, "tailscale ip -4", logger, as_root=True)
    else:
        exec_target.run_as_root(ts_cmd)
        result = exec_target.run_as_root("tailscale ip -4")

    tailscale_ip = result.stdout.strip()
    typer.echo(f"  Tailscale IP: {tailscale_ip}")
    db.update_vm_tailscale(vm_name, tailscale_ip)
    return tailscale_ip


def _describe_transport(exec_target: ExecTarget) -> str:
    """Return a short description of the transport used by an ExecTarget."""
    if exec_target.ssh is not None:
        return f"ssh:{exec_target.ssh.host}"
    if exec_target.lima is not None:
        return f"lima:{exec_target.lima.vm_name}"
    if exec_target.remote_lima is not None:
        return f"remote-lima:{exec_target.remote_lima.vm_name}"
    if exec_target.wsl2 is not None:
        return f"wsl2:{exec_target.wsl2.distro_name}"
    return "unknown"


def initialize_vm(
    db: Database,
    config: Config,
    vm_name: str,
    exec_target: ExecTarget,
    providers: dict[str, GitCredentialProvider],
    *,
    is_wsl2: bool = False,
    admin_username: str = "agentworks",
    tailscale_auth_key: str | None = None,
    git_tokens: dict[str, str] | None = None,
    bootstrap_complete: bool = False,
    tailscale_ip: str | None = None,
    on_tailscale_ready: Callable[[], None] | None = None,
) -> None:
    """Run the full initialization sequence on a newly provisioned VM.

    Phase A (bootstrap) steps are fatal -- any failure aborts initialization.
    Phase B (setup) steps are non-fatal -- failures are logged as warnings
    and the VM gets 'partial' status instead of 'complete'.
    """
    from dataclasses import replace

    from agentworks.ssh import SSHLogger

    home = f"/home/{admin_username}"
    logger = SSHLogger(vm_name, "vm-create")
    if tailscale_auth_key:
        logger.add_redaction(tailscale_auth_key)
    if git_tokens:
        for token in git_tokens.values():
            logger.add_redaction(token)

    # Attach logger to the provisioning transport
    exec_target = replace(exec_target, logger=logger)

    transport = _describe_transport(exec_target)

    try:
        db.insert_vm_event(vm_name, "provisioning_started", transport)
        ts_target = _phase_a_bootstrap(
            db,
            config,
            vm_name,
            exec_target,
            home,
            admin_username,
            is_wsl2,
            logger,
            tailscale_auth_key=tailscale_auth_key,
            bootstrap_complete=bootstrap_complete,
            tailscale_ip=tailscale_ip,
        )
        db.insert_vm_event(vm_name, "provisioning_complete", ts_target.ssh.host if ts_target.ssh else None)
    except Exception as e:
        db.update_vm_provisioning_status(vm_name, ProvisioningStatus.FAILED)
        db.insert_vm_event(vm_name, "provisioning_failed", str(e))
        logger.close()
        typer.echo(f"  Log: {logger.path}", err=True)
        raise

    # Tailscale is up; caller can clean up provisioning-only resources
    # (e.g., detach Azure public IP since Phase B uses Tailscale SSH).
    # Removing the public IP can destabilize the network stack briefly,
    # so we wait for Tailscale SSH to be reliably reachable before
    # proceeding with Phase B.
    if on_tailscale_ready is not None:
        try:
            on_tailscale_ready()
        except Exception as e:
            typer.echo(f"  Warning: post-provisioning cleanup failed: {e}", err=True)

        # Wait for Tailscale SSH to reconnect after network changes
        from agentworks.ssh import wait_for_reconnect

        wait_for_reconnect(ts_target)

    run_initialization(
        db,
        config,
        vm_name,
        ts_target,
        providers,
        home,
        admin_username,
        logger,
        git_tokens=git_tokens,
    )


def run_initialization(
    db: Database,
    config: Config,
    vm_name: str,
    ts_target: ExecTarget,
    providers: dict[str, GitCredentialProvider],
    home: str,
    admin_username: str,
    logger: SSHLogger,
    *,
    git_tokens: dict[str, str] | None = None,
) -> None:
    """Run Phase B (initialization) with status tracking and event logging.

    This is called both from initialize_vm() after provisioning and
    from reinit_vm() for repeatable re-initialization.
    """
    db.insert_vm_event(vm_name, "init_started")

    try:
        _phase_b_setup(
            db,
            config,
            vm_name,
            ts_target,
            providers,
            home,
            admin_username,
            logger,
            git_tokens=git_tokens,
        )
    except Exception as e:
        db.update_vm_init_status(vm_name, InitStatus.FAILED)
        db.insert_vm_event(vm_name, "init_failed", str(e))
        logger.close()
        raise

    if logger.has_warnings:
        db.update_vm_init_status(vm_name, InitStatus.PARTIAL)
        db.insert_vm_event(vm_name, "init_partial", f"{len(logger.warnings)} warning(s)")
    else:
        db.update_vm_init_status(vm_name, InitStatus.COMPLETE)
        db.insert_vm_event(vm_name, "init_complete")

    logger.close()


def _phase_a_bootstrap(
    db: Database,
    config: Config,
    vm_name: str,
    exec_target: ExecTarget,
    home: str,
    admin_username: str,
    is_wsl2: bool,
    logger: SSHLogger,
    *,
    tailscale_auth_key: str | None = None,
    bootstrap_complete: bool = False,
    tailscale_ip: str | None = None,
) -> ExecTarget:
    """Phase A: Bootstrap (over provisioning transport). All steps are fatal.

    Three paths depending on how much the provisioner already handled:

    1. bootstrap_complete=True (Lima/Azure): The provisioner already ran the
       full bootstrap. Skip straight to Tailscale SSH verification.
    2. Otherwise (WSL2): Run full bootstrap script over the provisioning
       transport (user, packages, SSH key, swap, Tailscale).

    Returns the Tailscale ExecTarget for Phase B.
    """
    db.update_vm_provisioning_status(vm_name, ProvisioningStatus.IN_PROGRESS)

    if bootstrap_complete and tailscale_ip:
        # Lima/Azure: provisioner already ran the full bootstrap.
        # Just update DB and move on to SSH verification.
        logger.step("Bootstrap (provisioner)")
        logger.output(f"Tailscale IP: {tailscale_ip}")
        db.update_vm_tailscale(vm_name, tailscale_ip)
        db.update_vm_provisioning_status(vm_name, ProvisioningStatus.COMPLETE)
    else:
        # WSL2: run bootstrap script over the provisioning transport
        tailscale_ip = _run_bootstrap_script(
            db,
            config,
            vm_name,
            exec_target,
            admin_username,
            is_wsl2,
            logger,
            tailscale_auth_key=tailscale_auth_key,
        )

    # Switch to Tailscale SSH, carrying over the SSH logger.
    # On Windows, force TTY to prevent zsh/login shell pipe hangs.
    import sys

    ts_target = ExecTarget(
        ssh=SSHTarget(
            host=tailscale_ip,
            user=admin_username,
            identity_file=config.user.ssh_private_key,
            force_tty=sys.platform == "win32",
        ),
        default_timeout=60,
        logger=exec_target.logger,
    )

    # Verify Tailscale SSH works (retry -- peer connection may take time)
    logger.step("Verify Tailscale SSH")
    typer.echo("  Verifying Tailscale SSH...")
    import time

    for attempt in range(5):
        try:
            _run_logged(ts_target, "echo ok", logger, timeout=15)
            break
        except SSHError:
            if attempt == 4:
                raise
            typer.echo(f"  Tailscale SSH not ready, retrying ({attempt + 1}/5)...")
            time.sleep(3)

    return ts_target


def _run_bootstrap_script(
    db: Database,
    config: Config,
    vm_name: str,
    exec_target: ExecTarget,
    admin_username: str,
    is_wsl2: bool,
    logger: SSHLogger,
    *,
    tailscale_auth_key: str | None = None,
) -> str:
    """Generate, copy, and run a bootstrap script on the VM. Returns Tailscale IP.

    Used for WSL2 where the bootstrap cannot be embedded in a provisioner's
    native mechanism (Lima provision block, Azure cloud-init).
    """
    import tempfile

    from agentworks.vms.bootstrap_script import generate_bootstrap_script, parse_bootstrap_output, vm_hostname

    typer.echo("Bootstrapping VM (detached)...")

    # Resolve Tailscale auth key
    ts_auth_key = _resolve_tailscale_auth_key(tailscale_auth_key)

    ssh_public_key = config.user.ssh_public_key.read_text().strip()
    # Determine platform for hostname. Look up the VM record for the actual
    # platform; fall back to transport-based detection.
    platform = "wsl2" if is_wsl2 else "unknown"
    vm_row = db.get_vm(vm_name)
    if vm_row is not None:
        platform = vm_row.platform
    script = generate_bootstrap_script(
        admin_username=admin_username,
        ssh_public_key=ssh_public_key,
        provisioning_packages=PROVISIONING_PACKAGES,
        tailscale_auth_key=ts_auth_key,
        hostname=vm_hostname(platform, vm_name),
        swap=0 if is_wsl2 else config.vm.swap,  # WSL2 provisioner handles swap
        is_wsl2=is_wsl2,
    )

    # Copy script to VM and execute via detached nohup
    remote_script = "/tmp/agentworks-bootstrap.sh"
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".sh", delete=False) as f:
        f.write(script.encode("utf-8"))
        local_script = f.name

    try:
        exec_target.copy_to(local_script, remote_script)
    finally:
        import os

        os.unlink(local_script)

    from agentworks.remote_exec import run_detached

    typer.echo("  Running bootstrap script...")
    detached = run_detached(
        exec_target,
        f"sudo -n /bin/bash {remote_script}",
        label="Bootstrap",
        base_path=f"/tmp/agentworks-bootstrap-{vm_name}",
        quiet=True,  # we parse the structured output ourselves
    )
    exec_target.run_as_root(f"rm -f {remote_script}", check=False)

    # Parse structured output
    bootstrap = parse_bootstrap_output(detached.output, detached.exit_code)

    # Feed results into logger and console
    for step in bootstrap.steps:
        logger.step(step.name)
        if step.success_msg:
            typer.echo(f"  {step.name}: {step.success_msg}")
            logger.output(step.success_msg)
        for warning in step.warnings:
            typer.echo(f"  Warning: {warning}", err=True)
            logger.warning(warning)
        if step.error:
            typer.echo(f"  Error: {step.error}", err=True)
            logger.log_error(step.error)

    # Log full output for troubleshooting
    if detached.output:
        logger.output(detached.output)

    if not bootstrap.ok:
        msg = f"Bootstrap script failed (exit {detached.exit_code})"
        if detached.output:
            msg += f"\n{detached.output[-500:]}"
        raise SSHError(msg)

    # Update DB with Tailscale info
    assert bootstrap.tailscale_ip is not None
    tailscale_ip = bootstrap.tailscale_ip
    typer.echo(f"  Tailscale IP: {tailscale_ip}")
    db.update_vm_tailscale(vm_name, tailscale_ip)
    db.update_vm_provisioning_status(vm_name, ProvisioningStatus.COMPLETE)

    return tailscale_ip


def _resolve_tailscale_auth_key(tailscale_auth_key: str | None = None) -> str:
    """Resolve Tailscale auth key from argument, env var, or prompt."""
    import os

    key = tailscale_auth_key or os.environ.get("TAILSCALE_AUTH_KEY")
    if key:
        return key
    from agentworks.prompt import prompt_secret

    return prompt_secret(
        "  Tailscale auth key",
        hint="Generate a key at https://login.tailscale.com/admin/settings/keys",
    )


def _phase_b_setup(
    db: Database,
    config: Config,
    vm_name: str,
    ts_target: ExecTarget,
    providers: dict[str, GitCredentialProvider],
    home: str,
    admin_username: str,
    logger: SSHLogger,
    *,
    git_tokens: dict[str, str] | None = None,
) -> None:
    """Phase B: Setup (over Tailscale SSH). Non-fatal steps warn and continue."""
    from agentworks.catalog import load_catalog, validate_selections

    typer.echo("Initializing VM...")
    db.update_vm_init_status(vm_name, InitStatus.IN_PROGRESS)
    catalog = load_catalog(config)
    validate_selections(config, catalog)

    # Non-fatal: system repos + packages (mise repo added, then all packages)
    _install_system_packages(ts_target, logger)

    # Non-fatal: apt sources required by selected apt_packages
    _configure_apt_sources(ts_target, config, catalog, logger)

    # Non-fatal: apt packages (direct list + catalog entries)
    _install_apt_packages(ts_target, config, catalog, logger)

    # Non-fatal: snap packages
    if config.vm.snap:
        logger.step("Snap packages")
        typer.echo(f"  Installing {len(config.vm.snap)} snap packages...")
        for pkg in config.vm.snap:
            try:
                _run_logged(ts_target, f"snap install {shlex.quote(pkg)}", logger, as_root=True, timeout=120)
            except SSHError as e:
                msg = f"snap install '{pkg}' failed: {e}"
                logger.warning(msg)
                typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: set default shell (before install commands so installers
    # write to the correct rc file)
    logger.step("Shell configuration")
    admin_shell = config.admin.shell
    typer.echo(f"  Setting shell to {admin_shell}...")
    try:
        # Touch .zshrc before chsh to prevent zsh's first-run wizard
        # (zsh-newuser-install) from prompting interactively on next login
        if admin_shell == "zsh":
            _run_logged(ts_target, f"touch {home}/.zshrc", logger, check=False)
        _run_logged(
            ts_target,
            f"usermod -s $(which {shlex.quote(admin_shell)}) {shlex.quote(admin_username)}",
            logger,
            as_root=True,
        )
    except SSHError as e:
        msg = f"shell configuration failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: reconcile authorized_keys
    _reconcile_authorized_keys(ts_target, config, home, logger)

    # Non-fatal: workspaces directory with ACLs for group-writable files.
    # Default ACLs ensure new files/dirs inherit group rwx regardless of umask.
    # Access ACLs fix existing files. Applied recursively to cover all workspaces.
    workspaces_dir = config.paths.vm_workspaces
    if workspaces_dir.startswith("/home/"):
        typer.echo(
            f"  Warning: vm_workspaces is under /home ({workspaces_dir}). "
            "This may require the home directory to be world-traversable.",
            err=True,
        )
    try:
        # acl is now installed as a system package in _install_system_packages
        _run_logged(ts_target, f"mkdir -p {workspaces_dir}", logger, as_root=True)
        # Ensure all parent directories are traversable by agents
        _run_logged(
            ts_target,
            f'sh -c \'p={workspaces_dir}; while [ "$p" != "/" ]; do chmod a+x "$p"; p=$(dirname "$p"); done\'',
            logger,
            as_root=True,
        )
        # Default ACLs on directories only (setfacl -R -d warns on files)
        _run_logged(
            ts_target,
            f"find {workspaces_dir} -type d -exec setfacl -d -m g::rwx -m m::rwx {{}} +",
            logger,
            as_root=True,
            timeout=120,
        )
        # Access ACLs on all existing files and dirs
        _run_logged(
            ts_target,
            f"setfacl -R -m g::rwx -m m::rwx {workspaces_dir}",
            logger,
            as_root=True,
            timeout=120,
        )
    except SSHError as e:
        msg = f"workspaces directory setup failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: agent tmux socket directory infrastructure.
    # Creates the shared group, root directory, and per-agent subdirectories.
    try:
        from agentworks.sessions.tmux import (
            cleanup_stale_sockets,
            ensure_agent_socket_dir,
            ensure_agent_socket_root,
        )

        logger.step("Agent tmux socket directories")
        typer.echo("  Setting up agent tmux socket infrastructure...")

        def _root_cmd(command: str, *, check: bool = True) -> object:
            return _run_logged(ts_target, command, logger, as_root=True, check=check)

        ensure_agent_socket_root(_root_cmd, admin_username)
        for agent in db.list_agents(vm_name):
            ensure_agent_socket_dir(_root_cmd, agent.linux_user)
            removed = cleanup_stale_sockets(_root_cmd, agent.linux_user)
            if removed:
                typer.echo(f"  Cleaned up {removed} stale socket(s) for {agent.linux_user}")
    except SSHError as e:
        msg = f"agent tmux socket setup failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: system install commands
    system_path = _run_catalog_commands(
        ts_target,
        config.vm.system_install_commands,
        catalog.system_install_commands,
        admin_shell,
        home,
        logger,
        label="System install command",
    )

    # Non-fatal: mise config (written before dotfiles so dotfiles can override)
    mise_path: list[str] = _mise_shims_path(home)
    if config.admin.mise_packages:
        _write_mise_config(ts_target, config.admin.mise_packages, config.admin.mise_install_before, home, logger)

    # Non-fatal: git safe.directory wildcard (disables ownership checks for the
    # multi-user workspace model where agents access repos owned by admin)
    if config.admin.git_force_safe_directory:
        try:
            _run_logged(ts_target, "git config --global --add safe.directory '*'", logger)
            typer.echo("  Git safe.directory wildcard configured")
        except SSHError as e:
            msg = f"git safe.directory setup failed: {e}"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: git credentials (before dotfiles and mise lockfile for private repos)
    if providers:
        _configure_git_credentials(vm_name, ts_target, providers, logger, git_tokens=git_tokens)

    # Non-fatal: dotfiles (can override mise config, can provide lockfile)
    if config.admin.dotfiles_source:
        logger.step("Dotfiles")
        dest = config.admin.dotfiles_destination.replace("~", home)
        try:
            from agentworks.sources import SourceRefError, fetch_dir, parse_source_ref

            ref = parse_source_ref(config.admin.dotfiles_source)
            typer.echo(f"  Syncing dotfiles from {config.admin.dotfiles_source}...")
            fetch_dir(ref, ts_target, dest, logger=logger)

            typer.echo(f"  Running dotfiles install: {config.admin.dotfiles_install_cmd}")
            _run_logged(ts_target, f"cd {dest} && {config.admin.dotfiles_install_cmd}", logger, timeout=120)
        except (SourceRefError, Exception) as e:
            msg = f"dotfiles install failed: {e}"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: mise lockfile (after git creds and dotfiles; overrides dotfiles lockfile)
    if config.admin.mise_lockfile:
        _fetch_mise_lockfile(ts_target, config.admin.mise_lockfile, home, logger)

    # Non-fatal: mise install (after config + dotfiles + lockfile are all settled)
    prune = config.admin.mise_prune_on_reinit
    if config.admin.mise_packages or config.admin.mise_lockfile:
        _run_mise_install(ts_target, admin_shell, home, config.admin.mise_allow_unlocked, logger, prune=prune)
    else:
        try:
            check = ts_target.run(f"test -f {home}/.config/mise/config.toml", check=False)
            if check.ok:
                _run_mise_install(ts_target, admin_shell, home, config.admin.mise_allow_unlocked, logger, prune=prune)
        except SSHError:
            pass

    # Non-fatal: user install commands for admin user (may depend on mise tools)
    user_path = _run_catalog_commands(
        ts_target,
        config.admin.user_install_commands,
        catalog.user_install_commands,
        admin_shell,
        home,
        logger,
        label="User install command",
    )

    # Non-fatal: shell profile (PATH exports, sourced at login)
    all_paths = system_path + mise_path + user_path
    _write_agentworks_profile(ts_target, all_paths, logger)

    # Non-fatal: shell rc (interactive shell hooks like mise activate)
    rc_snippets = [MISE_ACTIVATE_LINES] if config.admin.mise_activate else ["# mise activation disabled"]
    _write_agentworks_rc(ts_target, rc_snippets, logger)

    # Non-fatal: nerf tools
    if config.vm.nerf_build_claude_plugin:
        _build_nerf_claude_plugin(ts_target, config, logger)

    # Non-fatal: install nerf Claude plugin for admin user
    if config.admin.nerf_install_claude_plugin:
        _install_nerf_claude_plugin_for_user(ts_target, admin_shell, logger)


def _build_nerf_claude_plugin(
    ts_target: ExecTarget,
    config: Config,
    logger: SSHLogger,
) -> None:
    """Build the nerf Claude Code plugin locally and deploy to the VM. Non-fatal."""
    logger.step("Nerf tools (Claude plugin)")
    typer.echo("  Building nerf Claude Code plugin...")

    nerf_home = config.vm.nerf_home_dir
    plugin_dir = f"{nerf_home}/claude-plugin"

    try:
        try:
            from nerftools import BUILTIN_MANIFESTS_DIR  # type: ignore[import-untyped]
            from nerftools.formats import build_claude_plugin  # type: ignore[import-untyped]
            from nerftools.manifest import (  # type: ignore[import-untyped]
                ManifestError,
                load_manifest,
                merge_manifests,
            )
        except ImportError as e:
            raise RuntimeError(f"nerftools is not installed: {e}") from e

        manifest_paths: list[Path] = []
        if not config.vm.skip_nerf_defaults and BUILTIN_MANIFESTS_DIR.exists():
            for f in sorted(BUILTIN_MANIFESTS_DIR.iterdir()):
                if f.suffix == ".yaml" and f.is_file():
                    manifest_paths.append(f)
        manifest_paths.extend(config.vm.nerf_addl_manifests)

        try:
            manifests = merge_manifests([load_manifest(p) for p in manifest_paths])
        except ManifestError as e:
            raise RuntimeError(f"nerf manifest error: {e}") from e

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_claude_plugin(manifests, tmp_path)

            # Clean and create remote dir
            _run_logged(ts_target, f"rm -rf {shlex.quote(plugin_dir)}", logger, as_root=True)
            _run_logged(ts_target, f"mkdir -p {shlex.quote(plugin_dir)}", logger, as_root=True)
            _run_logged(ts_target, f"sudo chown -R $(id -un):$(id -un) {shlex.quote(plugin_dir)}", logger)

            # Copy plugin artifacts
            ts_target.copy_dir_to(tmp_path, plugin_dir, delete=False, timeout=60)

            # Make the entire nerf home world-readable so all users can access the plugin
            _run_logged(
                ts_target,
                f"chmod -R a+rX {shlex.quote(nerf_home)}",
                logger,
                as_root=True,
            )
            # Fix execute bits on scripts (Windows tarballs lose them, a+rX only sets x on dirs)
            _run_logged(
                ts_target,
                f"find {shlex.quote(plugin_dir)} -name 'nerf-*' -o -name 'nerfctl-*' | xargs -r chmod a+x",
                logger,
            )

        typer.echo(f"  Nerf Claude plugin built to {plugin_dir}")

        # System-wide env var so all users can locate nerf home
        env_line = f'export AGENTWORKS_NERF_HOME="{nerf_home}"'
        _run_logged(
            ts_target,
            f"printf '%s\\n' {shlex.quote(env_line)} | sudo tee /etc/profile.d/agentworks-nerf.sh > /dev/null",
            logger,
        )
        _run_logged(
            ts_target,
            f"grep -qF AGENTWORKS_NERF_HOME /etc/zsh/zprofile 2>/dev/null"
            f" || printf '%s\\n' {shlex.quote(env_line)} | sudo tee -a /etc/zsh/zprofile > /dev/null",
            logger,
        )

    except (SSHError, RuntimeError) as e:
        msg = f"nerf Claude plugin build failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)


def _install_nerf_claude_plugin_for_user(
    target: ExecTarget,
    shell: str,
    logger: SSHLogger,
) -> None:
    """Install the nerf Claude Code plugin for the current user. Non-fatal."""
    logger.step("Nerf plugin install")

    try:
        # Check that the plugin and install script exist via the system env var
        check_result = _run_logged(
            target,
            f"{shell} -lc 'test -x $AGENTWORKS_NERF_HOME/claude-plugin/scripts/nerfctl-install-plugin'",
            logger,
            check=False,
        )
        if not check_result.ok:
            typer.echo(
                "  Warning: nerf Claude plugin not found on this VM. "
                "Set nerf_build_claude_plugin = true in your VM template and reinit.",
                err=True,
            )
            return

        typer.echo("  Installing nerf Claude plugin...")
        _run_logged(
            target,
            f"{shell} -lc '$AGENTWORKS_NERF_HOME/claude-plugin/scripts/nerfctl-install-plugin'",
            logger,
            timeout=30,
        )
        typer.echo("  Nerf Claude plugin installed")
    except SSHError as e:
        msg = f"nerf plugin install failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)


def _configure_git_credentials(
    vm_name: str,
    ts_target: ExecTarget,
    providers: dict[str, GitCredentialProvider],
    logger: SSHLogger,
    git_tokens: dict[str, str] | None = None,
) -> None:
    """Configure git credential store on the VM with pre-collected or prompted tokens."""
    logger.step("Git credentials")
    typer.echo("  Configuring git credentials...")

    tokens = git_tokens or {}

    # Collect credential lines from all providers
    credential_lines: list[str] = []
    for name, provider in providers.items():
        try:
            token = tokens.get(name) or provider.obtain_token(vm_name)
            credential_lines.extend(provider.credential_lines(token))
        except Exception as e:
            msg = f"git credential setup failed for {name}: {e}"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)

    if not credential_lines:
        return

    # Write credentials and configure git on the VM
    try:
        cred_content = "\n".join(credential_lines) + "\n"
        ts_target.write_file("~/.git-credentials", cred_content, mode="600")
        _run_logged(
            ts_target,
            "git config --global credential.helper store",
            logger,
        )
        typer.echo(f"  Git credentials configured for {len(providers)} provider(s)")
    except SSHError as e:
        msg = f"git credential store setup failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)
