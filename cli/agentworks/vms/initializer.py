"""Uniform VM initialization -- platform-agnostic, Tailscale-first.

Two phases:
  A. Bootstrap (over provisioning transport): user, system packages, SSH key, Tailscale
  B. Setup (over Tailscale SSH): user packages, install commands, git credentials, dotfiles

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
    from agentworks.git_credentials.base import GitCredentialProvider
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


def _run_install_commands(
    target: ExecTarget,
    command_names: list[str],
    config: Config,
    logger: InitLogger,
) -> list[str]:
    """Run install commands under the user's configured shell.

    Returns accumulated PATH additions from all commands.
    """
    if not command_names:
        return []

    shell = config.user.shell
    path_additions: list[str] = []
    total = len(command_names)

    for i, name in enumerate(command_names, 1):
        cmd_config = config.install_commands[name]
        logger.step(f"Install command {i}/{total}: {name}")
        truncated = cmd_config.command[:60]
        typer.echo(f"  Install command {i}/{total} ({name}): {truncated}...")
        try:
            _run_logged(target, f"{shell} -lc '{cmd_config.command}'", logger, timeout=120)
        except SSHError as e:
            msg = f"install command '{name}' failed: {truncated}... ({e})"
            logger.warning(msg)
            typer.echo(f"  Warning: {msg}", err=True)
        path_additions.extend(cmd_config.path)

    return path_additions


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
    config: Config, credential_names: list[str] | None = None,
) -> dict[str, GitCredentialProvider]:
    """Resolve git credential provider instances from config."""
    from agentworks.git_credentials.azdo import AzDOCredentialProvider
    from agentworks.git_credentials.github import GitHubCredentialProvider

    names = credential_names or config.defaults.git_credentials or []
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


def initialize_vm(
    db: Database,
    config: Config,
    vm_name: str,
    exec_target: ExecTarget,
    providers: dict[str, GitCredentialProvider],
    *,
    extra_packages: list[str] | None = None,
    is_wsl2: bool = False,
    vm_user: str = "agentworks",
    tailscale_auth_key: str | None = None,
    git_tokens: dict[str, str] | None = None,
) -> None:
    """Run the full initialization sequence on a newly provisioned VM.

    Phase A (bootstrap) steps are fatal -- any failure aborts initialization.
    Phase B (setup) steps are non-fatal -- failures are logged as warnings
    and the VM gets 'partial' status instead of 'complete'.
    """
    home = f"/home/{vm_user}"
    logger = InitLogger(vm_name)

    try:
        ts_target = _phase_a_bootstrap(
            db, config, vm_name, exec_target, home, vm_user, is_wsl2, logger,
            tailscale_auth_key=tailscale_auth_key,
        )
        _phase_b_setup(
            db, config, vm_name, ts_target, providers, home, vm_user, extra_packages, logger,
            git_tokens=git_tokens,
        )
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

    typer.echo("Phase A: Bootstrap...")
    db.update_vm_init_status(vm_name, InitStatus.BOOTSTRAPPING)

    # Resolve Tailscale auth key
    ts_auth_key = _resolve_tailscale_auth_key(tailscale_auth_key)

    # Generate the bootstrap script
    ssh_public_key = config.user.ssh_public_key.read_text().strip()
    script = generate_bootstrap_script(
        vm_user=vm_user,
        ssh_public_key=ssh_public_key,
        system_packages=SYSTEM_PACKAGES,
        tailscale_auth_key=ts_auth_key,
        is_wsl2=is_wsl2,
    )

    # Copy script to VM and execute
    remote_script = "/tmp/agentworks-bootstrap.sh"
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".sh", delete=False) as f:
        f.write(script.encode("utf-8"))
        local_script = f.name

    try:
        exec_target.copy_to(local_script, remote_script)
    finally:
        import os
        os.unlink(local_script)

    typer.echo("  Running bootstrap script...")
    result = exec_target.run_as_root(f"/bin/bash {remote_script}", check=False, timeout=300)
    exec_target.run_as_root(f"rm -f {remote_script}", check=False)

    # Parse structured output
    bootstrap = parse_bootstrap_output(result.stdout, result.returncode)

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
    if result.stdout:
        logger.output(result.stdout)
    if result.stderr:
        logger.output(result.stderr)

    if not bootstrap.ok:
        msg = f"Bootstrap script failed (exit {result.returncode})"
        if result.stderr:
            msg += f": {result.stderr.strip()[:200]}"
        raise SSHError(msg)

    # Update DB with Tailscale info
    assert bootstrap.tailscale_ip is not None
    tailscale_ip = bootstrap.tailscale_ip
    typer.echo(f"  Tailscale IP: {tailscale_ip}")
    db.update_vm_tailscale(vm_name, tailscale_ip)
    db.update_vm_init_status(vm_name, InitStatus.TAILSCALE_UP)

    # Switch to Tailscale SSH
    ts_target = ExecTarget(
        ssh=SSHTarget(
            host=tailscale_ip,
            user=vm_user,
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
    vm_user: str,
    extra_packages: list[str] | None,
    logger: InitLogger,
    *,
    git_tokens: dict[str, str] | None = None,
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
            _run_logged(ts_target, f"apt-get install -y -qq {apt_str}", logger, as_root=True, timeout=300)
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
                _run_logged(ts_target, f"snap install {pkg}", logger, as_root=True, timeout=120)
            except SSHError as e:
                msg = f"snap install '{pkg}' failed: {e}"
                logger.warning(msg)
                typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: set default shell (before install commands so installers
    # write to the correct rc file)
    logger.step("Shell configuration")
    shell = config.user.shell
    typer.echo(f"  Setting shell to {shell}...")
    try:
        # Touch .zshrc before chsh to prevent zsh's first-run wizard
        # (zsh-newuser-install) from prompting interactively on next login
        if shell == "zsh":
            _run_logged(ts_target, f"touch {home}/.zshrc", logger, check=False)
        _run_logged(ts_target, f"chsh -s $(which {shell}) {vm_user}", logger, as_root=True)
    except SSHError as e:
        msg = f"shell configuration failed: {e}"
        logger.warning(msg)
        typer.echo(f"  Warning: {msg}", err=True)

    # Non-fatal: install commands (run under user's shell)
    path_additions = _run_install_commands(
        ts_target, config.vm.install_commands, config, logger,
    )

    # Non-fatal: PATH additions from install commands
    _write_path_additions(ts_target, path_additions, logger)

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
        except (SSHError, Exception) as e:
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
