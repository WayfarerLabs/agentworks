"""Uniform VM initialization -- platform-agnostic, Tailscale-first.

Two phases:
  A. Bootstrap (over provisioning transport): user, system packages, SSH key, Tailscale
  B. Setup (over Tailscale SSH): user packages, install commands, dotfiles, git host keys
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from agentworks.db import InitStatus
from agentworks.ssh import ExecTarget, SSHError, SSHTarget, rsync_to

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.git_hosts.base import GitHostProvider

SYSTEM_PACKAGES = ["openssh-server", "curl", "git", "sudo", "ca-certificates", "tmux", "tmuxinator"]


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


def initialize_vm(
    db: Database,
    config: Config,
    vm_name: str,
    exec_target: ExecTarget,
    providers: dict[str, GitHostProvider],
    *,
    extra_packages: list[str] | None = None,
    is_wsl2: bool = False,
) -> None:
    """Run the full initialization sequence on a newly provisioned VM."""

    # -- Phase A: Bootstrap (over provisioning transport) ------------------
    typer.echo("Phase A: Bootstrap...")
    db.update_vm_init_status(vm_name, InitStatus.BOOTSTRAPPING)

    # Step 1: Ensure agentworks user exists
    typer.echo("  Ensuring agentworks user...")
    exec_target.run_as_root(
        "bash -c 'id agentworks >/dev/null 2>&1 || useradd -m -s /bin/bash agentworks'",
        check=False,
    )
    exec_target.run_as_root(
        "usermod -aG sudo agentworks",
    )
    exec_target.run_as_root(
        "bash -c \"echo 'agentworks ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agentworks\"",
    )

    # Step 2: System packages
    typer.echo("  Installing system packages...")
    pkg_str = " ".join(SYSTEM_PACKAGES)
    exec_target.run_as_root("apt-get update -qq")
    exec_target.run_as_root(f"apt-get install -y -qq {pkg_str}")

    # Step 3: Add user's SSH public key
    typer.echo("  Adding SSH public key...")
    pub_key = config.user.ssh_public_key.read_text().strip()
    exec_target.run_as_root("mkdir -p /home/agentworks/.ssh")
    exec_target.run_as_root(f"bash -c \"echo '{pub_key}' >> /home/agentworks/.ssh/authorized_keys\"")
    exec_target.run_as_root("chown -R agentworks:agentworks /home/agentworks/.ssh")
    exec_target.run_as_root("chmod 700 /home/agentworks/.ssh")
    exec_target.run_as_root("chmod 600 /home/agentworks/.ssh/authorized_keys")

    # Step 4: Install and join Tailscale
    typer.echo("  Installing Tailscale...")
    exec_target.run_as_root("bash -c 'curl -fsSL https://tailscale.com/install.sh | sh'")

    import os

    ts_auth_key = os.environ.get("TAILSCALE_AUTH_KEY")
    if not ts_auth_key:
        typer.echo("  Generate a key at https://login.tailscale.com/admin/settings/keys")
        ts_auth_key = typer.prompt("  Tailscale auth key")
    ts_cmd = f"tailscale up --auth-key {ts_auth_key}"
    if is_wsl2:
        ts_cmd += " --userspace-networking"
    exec_target.run_as_root(ts_cmd)

    # Step 5: Read Tailscale IP and update DB
    result = exec_target.run_as_root("tailscale ip -4")
    tailscale_ip = result.stdout.strip()
    typer.echo(f"  Tailscale IP: {tailscale_ip}")
    db.update_vm_tailscale(vm_name, tailscale_ip)
    db.update_vm_init_status(vm_name, InitStatus.TAILSCALE_UP)

    # -- Switch to Tailscale SSH -------------------------------------------
    ts_target = ExecTarget(
        ssh=SSHTarget(
            host=tailscale_ip,
            user="agentworks",
            identity_file=config.user.ssh_private_key,
        )
    )

    # Verify Tailscale SSH works
    typer.echo("  Verifying Tailscale SSH...")
    ts_target.run("echo ok", timeout=30)

    # -- Phase B: Setup (over Tailscale SSH) -------------------------------
    typer.echo("Phase B: Setup...")
    db.update_vm_init_status(vm_name, InitStatus.INITIALIZING)

    # Step 6: User apt packages
    all_apt = config.vm.apt + (extra_packages or [])
    if all_apt:
        typer.echo(f"  Installing {len(all_apt)} apt packages...")
        apt_str = " ".join(all_apt)
        ts_target.run_as_root(f"apt-get install -y -qq {apt_str}")

    # Step 7: Snap packages
    if config.vm.snap:
        typer.echo(f"  Installing {len(config.vm.snap)} snap packages...")
        for pkg in config.vm.snap:
            ts_target.run_as_root(f"snap install {pkg}")

    # Step 8: Install commands (continue on failure)
    for i, cmd in enumerate(config.vm.install_commands, 1):
        typer.echo(f"  Install command {i}/{len(config.vm.install_commands)}: {cmd[:60]}...")
        try:
            ts_target.run(f"bash -lc '{cmd}'")
        except SSHError as e:
            typer.echo(f"  Warning: install command failed: {e}", err=True)

    # Step 9: Set default shell
    shell = config.user.shell
    typer.echo(f"  Setting shell to {shell}...")
    ts_target.run_as_root(f"chsh -s $(which {shell}) agentworks")

    # Step 10: Generate SSH keypair
    typer.echo("  Generating SSH keypair...")
    ts_target.run("ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''")
    result = ts_target.run("cat ~/.ssh/id_ed25519.pub")
    vm_pub_key = result.stdout.strip()
    db.update_vm_ssh_public_key(vm_name, vm_pub_key)

    # Step 11: Register SSH key with git hosts
    for gh_name, provider in providers.items():
        typer.echo(f"  Registering SSH key with {gh_name}...")
        remote_key_id = provider.register_key(vm_name, vm_pub_key)
        db.insert_vm_git_host_key(vm_name, gh_name, remote_key_id)

    # Step 12: Dotfiles
    if config.dotfiles.enabled and config.dotfiles.source.exists():
        typer.echo("  Copying dotfiles...")
        assert ts_target.ssh is not None
        rsync_to(ts_target.ssh, config.dotfiles.source, "/home/agentworks/.dotfiles")
        typer.echo(f"  Running dotfiles install: {config.dotfiles.install_cmd}")
        try:
            ts_target.run(f"cd ~/.dotfiles && {config.dotfiles.install_cmd}")
        except SSHError as e:
            typer.echo(f"  Warning: dotfiles install failed: {e}", err=True)

    # -- Done --------------------------------------------------------------
    db.update_vm_init_status(vm_name, InitStatus.COMPLETE)
    typer.echo(f"VM '{vm_name}' initialization complete")
