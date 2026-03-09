"""Uniform VM initialization -- platform-agnostic, Tailscale-first.

Two phases:
  A. Bootstrap (over provisioning transport): user, system packages, SSH key, Tailscale
  B. Setup (over Tailscale SSH): user packages, install commands, dotfiles, git host keys

Phase A steps are fatal -- if they fail, the VM is unreachable and useless.
Phase B steps are non-fatal -- failures produce warnings and a 'partial' status.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from agentworks.db import InitStatus
from agentworks.ssh import ExecTarget, SSHError, SSHTarget, rsync_to
from agentworks.vms.init_log import InitLogger

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.git_hosts.base import GitHostProvider
    from agentworks.ssh import SSHResult

SYSTEM_PACKAGES = ["openssh-server", "curl", "git", "sudo", "ca-certificates", "tmux", "tmuxinator"]


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


def resolve_git_host_providers(config: Config, git_host_names: list[str] | None = None) -> dict[str, GitHostProvider]:
    """Resolve git host provider instances from config."""
    from agentworks.git_hosts.azdo import AzDOProvider
    from agentworks.git_hosts.github import GitHubProvider

    names = git_host_names or config.defaults.git_hosts or list(config.git_hosts.keys())
    providers: dict[str, GitHostProvider] = {}
    for name in names:
        gh_config = config.git_hosts.get(name)
        if gh_config is None:
            raise typer.Exit(1)
        if gh_config.type == "azdo":
            assert gh_config.org is not None
            providers[name] = AzDOProvider(org=gh_config.org)
        elif gh_config.type == "github":
            providers[name] = GitHubProvider()
    return providers


def verify_git_host_auth(providers: dict[str, GitHostProvider]) -> None:
    """Pre-flight: verify auth for all selected git host providers."""
    for name, provider in providers.items():
        if not provider.verify_auth():
            typer.echo(f"Error: Authentication failed for git host '{name}'. {provider.auth_hint()}", err=True)
            raise typer.Exit(1)
    if providers:
        typer.echo(f"Git host auth verified: {', '.join(providers.keys())}")


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
) -> str:
    """Prompt for auth key, join Tailscale, update DB. Returns the Tailscale IP."""
    import os

    ts_auth_key = os.environ.get("TAILSCALE_AUTH_KEY")
    if not ts_auth_key:
        typer.echo("  Generate a key at https://login.tailscale.com/admin/settings/keys")
        ts_auth_key = str(typer.prompt("  Tailscale auth key"))
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


def initialize_vm(
    db: Database,
    config: Config,
    vm_name: str,
    exec_target: ExecTarget,
    providers: dict[str, GitHostProvider],
    *,
    extra_packages: list[str] | None = None,
    is_wsl2: bool = False,
    vm_user: str = "agentworks",
) -> None:
    """Run the full initialization sequence on a newly provisioned VM.

    Phase A (bootstrap) steps are fatal -- any failure aborts initialization.
    Phase B (setup) steps are non-fatal -- failures are logged as warnings
    and the VM gets 'partial' status instead of 'complete'.
    """
    home = f"/home/{vm_user}"
    logger = InitLogger(vm_name)

    try:
        ts_target = _phase_a_bootstrap(db, config, vm_name, exec_target, home, vm_user, is_wsl2, logger)
        _phase_b_setup(db, config, vm_name, ts_target, providers, home, vm_user, extra_packages, logger)
    except Exception:
        logger.close()
        raise

    # Determine final status
    if logger.has_warnings:
        db.update_vm_init_status(vm_name, InitStatus.PARTIAL)
        logger.close()
        typer.echo(f"\nVM '{vm_name}' initialization completed with {len(logger.warnings)} warning(s):")
        for w in logger.warnings:
            typer.echo(f"  - {w}")
        typer.echo(f"Init log: {logger.path}")
    else:
        db.update_vm_init_status(vm_name, InitStatus.COMPLETE)
        logger.close()
        typer.echo(f"VM '{vm_name}' initialization complete")


def _phase_a_bootstrap(
    db: Database,
    config: Config,
    vm_name: str,
    exec_target: ExecTarget,
    home: str,
    vm_user: str,
    is_wsl2: bool,
    logger: InitLogger,
) -> ExecTarget:
    """Phase A: Bootstrap (over provisioning transport). All steps are fatal.

    Returns the Tailscale ExecTarget for Phase B.
    """
    typer.echo("Phase A: Bootstrap...")
    db.update_vm_init_status(vm_name, InitStatus.BOOTSTRAPPING)

    # Step 1: Ensure user exists
    logger.step("Ensure user")
    typer.echo(f"  Ensuring user '{vm_user}'...")
    _run_logged(
        exec_target,
        f"bash -c 'id {vm_user} >/dev/null 2>&1 || useradd -m -s /bin/bash {vm_user}'",
        logger, as_root=True, check=False,
    )
    _run_logged(exec_target, f"usermod -aG sudo {vm_user}", logger, as_root=True)
    _run_logged(
        exec_target,
        f"bash -c \"echo '{vm_user} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/{vm_user}\"",
        logger, as_root=True,
    )

    # Step 2: System packages
    logger.step("System packages")
    typer.echo("  Installing system packages...")
    pkg_str = " ".join(SYSTEM_PACKAGES)
    _run_logged(exec_target, "apt-get update -qq", logger, as_root=True)
    _run_logged(exec_target, f"apt-get install -y -qq {pkg_str}", logger, as_root=True)

    # Step 3: Add user's SSH public key
    logger.step("SSH public key")
    typer.echo("  Adding SSH public key...")
    pub_key = config.user.ssh_public_key.read_text().strip()
    _run_logged(exec_target, f"mkdir -p {home}/.ssh", logger, as_root=True)
    _run_logged(
        exec_target,
        f"bash -c \"echo '{pub_key}' >> {home}/.ssh/authorized_keys\"",
        logger, as_root=True,
    )
    _run_logged(exec_target, f"chown -R {vm_user}:{vm_user} {home}/.ssh", logger, as_root=True)
    _run_logged(exec_target, f"chmod 700 {home}/.ssh", logger, as_root=True)
    _run_logged(exec_target, f"chmod 600 {home}/.ssh/authorized_keys", logger, as_root=True)

    # Step 4: Install and join Tailscale
    logger.step("Tailscale")
    typer.echo("  Installing Tailscale...")
    _run_logged(
        exec_target,
        "bash -c 'curl -fsSL https://tailscale.com/install.sh | sh'",
        logger, as_root=True,
    )

    tailscale_ip = _join_tailscale(db, vm_name, exec_target, is_wsl2=is_wsl2, logger=logger)
    db.update_vm_init_status(vm_name, InitStatus.TAILSCALE_UP)

    # Switch to Tailscale SSH
    ts_target = ExecTarget(
        ssh=SSHTarget(
            host=tailscale_ip,
            user=vm_user,
            identity_file=config.user.ssh_private_key,
        )
    )

    # Verify Tailscale SSH works
    logger.step("Verify Tailscale SSH")
    typer.echo("  Verifying Tailscale SSH...")
    _run_logged(ts_target, "echo ok", logger, timeout=30)

    return ts_target


def _phase_b_setup(
    db: Database,
    config: Config,
    vm_name: str,
    ts_target: ExecTarget,
    providers: dict[str, GitHostProvider],
    home: str,
    vm_user: str,
    extra_packages: list[str] | None,
    logger: InitLogger,
) -> None:
    """Phase B: Setup (over Tailscale SSH). Non-fatal steps warn and continue."""
    typer.echo("Phase B: Setup...")
    db.update_vm_init_status(vm_name, InitStatus.INITIALIZING)

    # Non-fatal: apt packages
    all_apt = config.vm.apt + (extra_packages or [])
    if all_apt:
        logger.step("User apt packages")
        typer.echo(f"  Installing {len(all_apt)} apt packages...")
        apt_str = " ".join(all_apt)
        try:
            _run_logged(ts_target, f"apt-get install -y -qq {apt_str}", logger, as_root=True)
        except SSHError as e:
            msg = f"apt packages failed: {e}"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: snap packages
    if config.vm.snap:
        logger.step("Snap packages")
        typer.echo(f"  Installing {len(config.vm.snap)} snap packages...")
        for pkg in config.vm.snap:
            try:
                _run_logged(ts_target, f"snap install {pkg}", logger, as_root=True)
            except SSHError as e:
                msg = f"snap install '{pkg}' failed: {e}"
                logger.warning(msg)
                typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: install commands
    for i, cmd in enumerate(config.vm.install_commands, 1):
        logger.step(f"Install command {i}/{len(config.vm.install_commands)}")
        typer.echo(f"  Install command {i}/{len(config.vm.install_commands)}: {cmd[:60]}...")
        try:
            _run_logged(ts_target, f"bash -lc '{cmd}'", logger)
        except SSHError as e:
            msg = f"install command failed: {cmd[:60]}... ({e})"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: set default shell
    logger.step("Shell configuration")
    shell = config.user.shell
    typer.echo(f"  Setting shell to {shell}...")
    try:
        _run_logged(ts_target, f"chsh -s $(which {shell}) {vm_user}", logger, as_root=True)
    except SSHError as e:
        msg = f"shell configuration failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: generate SSH keypair
    logger.step("SSH keypair generation")
    typer.echo("  Generating SSH keypair...")
    try:
        _run_logged(ts_target, "ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''", logger)
        result = _run_logged(ts_target, "cat ~/.ssh/id_ed25519.pub", logger)
        vm_pub_key = result.stdout.strip()
        db.update_vm_ssh_public_key(vm_name, vm_pub_key)
    except SSHError as e:
        msg = f"SSH keypair generation failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)
        vm_pub_key = None

    # Non-fatal: register SSH key with git hosts (skip if no keypair)
    if vm_pub_key:
        for gh_name, provider in providers.items():
            logger.step(f"Git host key: {gh_name}")
            typer.echo(f"  Registering SSH key with {gh_name}...")
            try:
                remote_key_id = provider.register_key(vm_name, vm_pub_key)
                db.insert_vm_git_host_key(vm_name, gh_name, remote_key_id)
            except Exception as e:
                msg = f"git host key registration failed for {gh_name}: {e}"
                logger.warning(msg)
                typer.echo(f"  Warning: {msg}", err=True)
    elif providers:
        msg = "skipped git host key registration (no SSH keypair)"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: dotfiles
    if config.dotfiles.enabled and config.dotfiles.source.exists():
        logger.step("Dotfiles")
        typer.echo("  Copying dotfiles...")
        try:
            assert ts_target.ssh is not None
            rsync_to(ts_target.ssh, config.dotfiles.source, f"{home}/.dotfiles")
            typer.echo(f"  Running dotfiles install: {config.dotfiles.install_cmd}")
            _run_logged(ts_target, f"cd ~/.dotfiles && {config.dotfiles.install_cmd}", logger)
        except (SSHError, Exception) as e:
            msg = f"dotfiles install failed: {e}"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)
