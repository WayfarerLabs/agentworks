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
from typing import TYPE_CHECKING

import typer

from agentworks.db import InitStatus, ProvisioningStatus
from agentworks.ssh import ExecTarget, SSHError, SSHTarget, rsync_to
from agentworks.vms.init_log import InitLogger

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.git_credentials.base import GitCredentialProvider
    from agentworks.ssh import SSHResult

SYSTEM_PACKAGES = [
    "openssh-server", "curl", "git", "sudo", "ca-certificates", "gnupg",
    "unzip", "tmux", "tmuxinator",
]


def _run_logged(
    target: ExecTarget,
    command: str,
    logger: InitLogger,
    *,
    as_root: bool = False,
    check: bool = True,
    timeout: int | None = None,
) -> SSHResult:
    """Run a command on the target and log the command + full output."""
    logger.output(f"$ {command}")
    result = target.run_as_root(command, check=check, timeout=timeout) if as_root else target.run(
        command, check=check, timeout=timeout,
    )
    if result.stdout:
        logger.output(result.stdout)
    if result.stderr:
        logger.output(result.stderr)
    return result



def _write_path_additions(
    target: ExecTarget,
    path_additions: list[str],
    logger: InitLogger,
) -> None:
    """Write accumulated PATH additions to $HOME/.agentworks-path.sh.

    Sources the file from ~/.profile (bash/sh) and ~/.zprofile (zsh).
    Uses $HOME instead of ~ throughout because tilde expansion is not
    reliable in all SSH/shell contexts.
    """
    if not path_additions:
        return

    logger.step("PATH configuration")
    typer.echo(f"  Adding {len(path_additions)} PATH entries...")

    try:
        # Build the path file content locally and copy it over
        # (avoids quoting issues with nested quotes over SSH on Windows)
        lines = ["# Managed by agentworks -- do not edit"]
        for p in path_additions:
            expanded = p.replace("~", "$HOME", 1) if p.startswith("~") else p
            lines.append(f'export PATH="{expanded}:$PATH"')
        target.write_file("~/.agentworks-path.sh", "\n".join(lines) + "\n")

        # Source from ~/.profile (bash/sh) and ~/.zprofile (zsh)
        source_line = ". $HOME/.agentworks-path.sh"
        for rc in ("$HOME/.profile", "$HOME/.zprofile"):
            _run_logged(
                target,
                f"grep -q agentworks-path.sh {rc} 2>/dev/null"
                f" || printf '%s\\n' '{source_line}' >> {rc}",
                logger,
            )
    except SSHError as e:
        msg = f"PATH configuration failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)


AUTHORIZED_KEYS_HEADER = """\
# Managed by agentworks -- manual edits will be overwritten on reinit.
# To add keys, use user.extra_ssh_public_keys in your agentworks config.
"""


def _reconcile_authorized_keys(
    target: ExecTarget,
    config: Config,
    home: str,
    logger: InitLogger,
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
    logger: InitLogger,
) -> None:
    """Configure apt sources required by selected apt_packages. Idempotent."""
    from agentworks.catalog import ResolvedCatalog

    assert isinstance(catalog, ResolvedCatalog)

    # Collect all apt sources needed by selected apt_packages
    required_sources: dict[str, object] = {}
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
        # Idempotent: skip if key already exists
        check = target.run(f"test -f {shlex.quote(src.key_path)}", check=False)
        if check.returncode == 0:
            typer.echo(f"  Apt source '{name}': already configured, skipping")
            logger.output(f"apt source {name}: key exists at {src.key_path}, skipping")
            continue

        typer.echo(f"  Configuring apt source '{name}'...")
        try:
            # Ensure parent directory for key_path exists
            from pathlib import PurePosixPath
            key_dir = str(PurePosixPath(src.key_path).parent)
            _run_logged(target, f"install -m 0755 -d {shlex.quote(key_dir)}", logger, as_root=True)

            # Download GPG key
            if src.key_dearmor:
                _run_logged(
                    target,
                    f"curl -fsSL {shlex.quote(src.key_url)}"
                    f" | gpg --dearmor -o {shlex.quote(src.key_path)}",
                    logger, as_root=True, timeout=60,
                )
            else:
                _run_logged(
                    target,
                    f"curl -fsSL {shlex.quote(src.key_url)}"
                    f" -o {shlex.quote(src.key_path)}",
                    logger, as_root=True, timeout=60,
                )
            _run_logged(target, f"chmod a+r {shlex.quote(src.key_path)}", logger, as_root=True)

            # Write source list
            resolved_source = src.source.replace("{arch}", arch)
            source_path = f"/etc/apt/sources.list.d/{src.source_file}"
            _run_logged(
                target,
                f"bash -c {shlex.quote(f'printf \"%s\\n\" {shlex.quote(resolved_source)} > {source_path}')}",
                logger, as_root=True,
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


def _install_apt_packages(
    target: ExecTarget,
    config: Config,
    catalog: object,
    logger: InitLogger,
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
        _run_logged(target, f"apt-get install -y -qq {apt_str}", logger, as_root=True, timeout=300)
    except SSHError as e:
        msg = f"apt packages failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)


def _build_test_command(entry: object, shell: str) -> str | None:
    """Build a shell command to check if an install command's tool is present.

    test_exec uses a login shell (-l) with interactive flag (-i) to ensure
    all profile/rc files are sourced, matching a real login session.
    """
    if getattr(entry, "test_exec", None):
        return f"{shell} -lic 'command -v {entry.test_exec}' > /dev/null 2>&1"
    if getattr(entry, "test_file", None):
        path = entry.test_file.replace("~", "$HOME", 1) if entry.test_file.startswith("~") else entry.test_file
        return f"test -f {path}"
    if getattr(entry, "test_dir", None):
        path = entry.test_dir.replace("~", "$HOME", 1) if entry.test_dir.startswith("~") else entry.test_dir
        return f"test -d {path}"
    return None


def _run_catalog_commands(
    target: ExecTarget,
    command_names: list[str],
    entries: dict[str, object],
    shell: str,
    logger: InitLogger,
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
        test_cmd = _build_test_command(entry, shell)
        if test_cmd:
            try:
                check = target.run(test_cmd, check=False, timeout=10)
                if check.returncode == 0:
                    typer.echo(f"  {label} {i}/{total} ({name}): already installed, skipping")
                    logger.output(f"{name}: test check passed, skipping")
                    path_additions.extend(entry.path)
                    continue
            except SSHError:
                # Timeout or connection issue -- assume not installed, proceed
                pass

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
    import subprocess

    try:
        result = subprocess.run(["tailscale", "status"], capture_output=True, text=True, timeout=10)
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
) -> dict[str, GitCredentialProvider]:
    """Resolve git credential provider instances from config."""
    from agentworks.git_credentials.azdo import AzDOCredentialProvider
    from agentworks.git_credentials.github import GitHubCredentialProvider

    names = config.defaults.git_credentials or []
    providers: dict[str, GitCredentialProvider] = {}
    if not names:
        typer.echo("Warning: no git credentials configured (set defaults.git_credentials in config)", err=True)
        return providers
    for name in names:
        cred_config = config.git_credentials.get(name)
        if cred_config is None:
            typer.echo(f"Error: git credential '{name}' not found in config", err=True)
            raise typer.Exit(1)
        if cred_config.type == "azdo":
            assert cred_config.org is not None
            providers[name] = AzDOCredentialProvider(config_name=name, org=cred_config.org)
        elif cred_config.type == "github":
            providers[name] = GitHubCredentialProvider(config_name=name)
    return providers


def verify_git_credential_auth(providers: dict[str, GitCredentialProvider]) -> None:
    """Pre-flight: verify auth for all selected git credential providers."""
    for name, provider in providers.items():
        if not provider.verify_auth():
            typer.echo(f"Error: Authentication check failed for '{name}'. {provider.auth_hint()}", err=True)
            raise typer.Exit(1)
    if providers:
        typer.echo(f"Git credentials configured: {', '.join(providers.keys())}")


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
    logger: InitLogger | None = None,
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
) -> None:
    """Run the full initialization sequence on a newly provisioned VM.

    Phase A (bootstrap) steps are fatal -- any failure aborts initialization.
    Phase B (setup) steps are non-fatal -- failures are logged as warnings
    and the VM gets 'partial' status instead of 'complete'.
    """
    home = f"/home/{admin_username}"
    logger = InitLogger(vm_name)
    if tailscale_auth_key:
        logger.add_redaction(tailscale_auth_key)
    if git_tokens:
        for token in git_tokens.values():
            logger.add_redaction(token)

    transport = _describe_transport(exec_target)

    try:
        db.insert_vm_event(vm_name, "provisioning_started", transport)
        ts_target = _phase_a_bootstrap(
            db, config, vm_name, exec_target, home, admin_username, is_wsl2, logger,
            tailscale_auth_key=tailscale_auth_key,
        )
        db.insert_vm_event(vm_name, "provisioning_complete", ts_target.ssh.host if ts_target.ssh else None)
    except Exception as e:
        db.update_vm_provisioning_status(vm_name, ProvisioningStatus.FAILED)
        db.insert_vm_event(vm_name, "provisioning_failed", str(e))
        logger.close()
        raise

    run_initialization(
        db, config, vm_name, ts_target, providers, home, admin_username,
        logger, git_tokens=git_tokens,
    )


def run_initialization(
    db: Database,
    config: Config,
    vm_name: str,
    ts_target: ExecTarget,
    providers: dict[str, GitCredentialProvider],
    home: str,
    admin_username: str,
    logger: InitLogger,
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
            db, config, vm_name, ts_target, providers, home, admin_username,
            logger, git_tokens=git_tokens,
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
    logger: InitLogger,
    *,
    tailscale_auth_key: str | None = None,
) -> ExecTarget:
    """Phase A: Bootstrap (over provisioning transport). All steps are fatal.

    Generates a self-contained bash script, copies it to the VM, and
    executes it as a single command. This avoids shell quoting issues
    across the various provisioning transports.

    Returns the Tailscale ExecTarget for Phase B.
    """
    import tempfile

    from agentworks.vms.bootstrap_script import generate_bootstrap_script, parse_bootstrap_output

    typer.echo("Bootstrapping VM (detached)...")
    db.update_vm_provisioning_status(vm_name, ProvisioningStatus.IN_PROGRESS)

    # Resolve Tailscale auth key
    ts_auth_key = _resolve_tailscale_auth_key(tailscale_auth_key)

    # Generate the bootstrap script
    ssh_public_key = config.user.ssh_public_key.read_text().strip()
    script = generate_bootstrap_script(
        admin_username=admin_username,
        ssh_public_key=ssh_public_key,
        system_packages=SYSTEM_PACKAGES,
        tailscale_auth_key=ts_auth_key,
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
            logger.error(step.error)

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

    # Switch to Tailscale SSH
    ts_target = ExecTarget(
        ssh=SSHTarget(
            host=tailscale_ip,
            user=admin_username,
            identity_file=config.user.ssh_private_key,
        ),
        default_timeout=60,
    )

    # Verify Tailscale SSH works
    logger.step("Verify Tailscale SSH")
    typer.echo("  Verifying Tailscale SSH...")
    _run_logged(ts_target, "echo ok", logger, timeout=30)

    return ts_target


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
    logger: InitLogger,
    *,
    git_tokens: dict[str, str] | None = None,
) -> None:
    """Phase B: Setup (over Tailscale SSH). Non-fatal steps warn and continue."""
    from agentworks.catalog import load_catalog, validate_selections

    typer.echo("Initializing VM...")
    db.update_vm_init_status(vm_name, InitStatus.IN_PROGRESS)
    catalog = load_catalog(config)
    validate_selections(config, catalog)

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
    admin_shell = config.vm.admin_shell
    typer.echo(f"  Setting shell to {admin_shell}...")
    try:
        # Touch .zshrc before chsh to prevent zsh's first-run wizard
        # (zsh-newuser-install) from prompting interactively on next login
        if admin_shell == "zsh":
            _run_logged(ts_target, f"touch {home}/.zshrc", logger, check=False)
        _run_logged(
            ts_target,
            f"chsh -s $(which {shlex.quote(admin_shell)}) {shlex.quote(admin_username)}",
            logger, as_root=True,
        )
    except SSHError as e:
        msg = f"shell configuration failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: reconcile authorized_keys
    _reconcile_authorized_keys(ts_target, config, home, logger)

    # Non-fatal: system install commands
    system_path = _run_catalog_commands(
        ts_target, config.vm.system_install_commands,
        catalog.system_install_commands, admin_shell, logger,
        label="System install command",
    )

    # Non-fatal: user install commands for admin user
    user_path = _run_catalog_commands(
        ts_target, config.vm.admin_install_commands,
        catalog.user_install_commands, admin_shell, logger,
        label="User install command",
    )

    # Non-fatal: PATH additions from both system + user install commands
    _write_path_additions(ts_target, system_path + user_path, logger)

    # Non-fatal: git credentials
    if providers:
        _configure_git_credentials(vm_name, ts_target, providers, logger, git_tokens=git_tokens)

    # Non-fatal: dotfiles
    if config.dotfiles.enabled and config.dotfiles.source.exists():
        logger.step("Dotfiles")
        typer.echo("  Copying dotfiles...")
        try:
            assert ts_target.ssh is not None
            rsync_to(ts_target.ssh, config.dotfiles.source, f"{home}/.dotfiles")
            typer.echo(f"  Running dotfiles install: {config.dotfiles.install_cmd}")
            _run_logged(ts_target, f"cd ~/.dotfiles && {config.dotfiles.install_cmd}", logger, timeout=120)
        except Exception as e:
            msg = f"dotfiles install failed: {e}"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)


def _configure_git_credentials(
    vm_name: str,
    ts_target: ExecTarget,
    providers: dict[str, GitCredentialProvider],
    logger: InitLogger,
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
