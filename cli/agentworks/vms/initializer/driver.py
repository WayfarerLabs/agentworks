"""The two-phase init driver: Phase A (bootstrap, over the provisioning
transport, fatal-on-failure) and Phase B (setup, over Tailscale SSH,
non-fatal-on-failure). ``initialize_vm`` runs both for a freshly
provisioned VM; ``run_initialization`` runs Phase B alone for
``vm reinit``.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.db import InitStatus, ProvisioningStatus
from agentworks.env import ResourceContext, vm_stable_identity_env
from agentworks.ssh import SSHError, SSHLogger
from agentworks.transports import SSHTransport, Transport

from .credentials import _configure_git_credentials
from .mise import (
    MISE_ACTIVATE_LINES,
    _fetch_mise_lockfile,
    _mise_shims_path,
    _run_mise_install,
    _write_mise_config,
)
from .packages import (
    _configure_apt_sources,
    _install_apt_packages,
    _install_system_packages,
    _run_install_commands,
)
from .shell_env import (
    _ensure_agentworks_files_sourced,
    _write_agentworks_identity_profile,
    _write_agentworks_profile,
    _write_agentworks_rc,
    _write_skel_seeds,
    _write_sshd_accept_env,
    _write_sudoers_console_setenv,
    _write_sudoers_env_keep,
)
from .ssh_keys import _apply_sve_mask, _preserve_ssh_host_keys, _reconcile_authorized_keys

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from agentworks.capabilities.git_credential.base import GitCredentialProvider
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.resources.registry import Registry
    from agentworks.vms.admin import AdminConfig
    from agentworks.vms.templates import ResolvedVMTemplate


def initialize_vm(
    db: Database,
    config: Config,
    registry: Registry,
    vm_template: ResolvedVMTemplate,
    admin: AdminConfig,
    vm_name: str,
    exec_target: Transport,
    providers: dict[str, GitCredentialProvider],
    platform: VMPlatform,
    *,
    hold_active: AbstractContextManager[None],
    admin_username: str = "agentworks",
    tailscale_auth_key: str,
    git_tokens: dict[str, str],
    bootstrap_complete: bool = False,
    tailscale_ip: str | None = None,
    on_tailscale_ready: Callable[[], None] | None = None,
) -> None:
    """Run the full initialization sequence on a newly provisioned VM.

    Phase A (bootstrap) steps are fatal: any failure aborts initialization.
    Phase B (setup) steps are non-fatal: failures are logged as warnings
    and the VM gets 'partial' status instead of 'complete'.

    Both ``tailscale_auth_key`` and ``git_tokens`` are required;
    ``create_vm`` resolves them via the framework at manager-entry and
    threads them in. ``hold_active`` is the keepalive span built at
    ``create_vm``'s composition root (``platform.vm_active``), anchoring
    the VM active for the whole init; ``platform`` still rides along for
    the WSL2 swap check below.
    """
    from agentworks.ssh import SSHLogger

    home = f"/home/{admin_username}"
    logger = SSHLogger(vm_name, "vm-create")
    logger.add_redaction(tailscale_auth_key)
    if git_tokens:
        for token in git_tokens.values():
            logger.add_redaction(token)

    # Attach logger to the provisioning transport. ``Transport`` declares
    # ``logger`` on the ABC; the assignment is polymorphic.
    exec_target.logger = logger

    transport = exec_target.describe()

    # Anchor the VM in an active state for the full init span. No-op for
    # Lima/Azure/Proxmox; WSL2 holds a wsl.exe subprocess open so the distro
    # doesn't idle-shut between Phase A (wsl.exe transport) and Phase B
    # (Tailscale SSH). The span is built at create_vm's composition root
    # and handed in; the VM was just provisioned and is running, so no
    # power-state convergence happens here, only the hold.
    vm_row = db.get_vm(vm_name)
    assert vm_row is not None, "create_vm inserts the row before init"
    with hold_active:
        try:
            db.insert_vm_event(vm_name, "provisioning_started", transport)
            ts_target = _phase_a_bootstrap(
                db,
                config,
                vm_template,
                vm_name,
                exec_target,
                home,
                admin_username,
                vm_row.hostname,
                logger,
                tailscale_auth_key=tailscale_auth_key,
                # WSL2 handles swap natively before bootstrap; every
                # other platform lets the script create the swapfile.
                script_swap=0 if platform.name == "wsl2" else vm_template.swap,
                bootstrap_complete=bootstrap_complete,
                tailscale_ip=tailscale_ip,
            )
            db.insert_vm_event(
                vm_name,
                "provisioning_complete",
                ts_target.host if isinstance(ts_target, SSHTransport) else None,
            )
        except Exception as e:
            db.update_vm_provisioning_status(vm_name, ProvisioningStatus.FAILED)
            db.insert_vm_event(vm_name, "provisioning_failed", str(e))
            logger.close()
            output.warn(f"Log: {logger.path}")
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
                output.warn(f"post-provisioning cleanup failed: {e}")

            # Wait for Tailscale SSH to reconnect after network changes
            from agentworks.transports import wait_for_reconnect

            wait_for_reconnect(ts_target)

        run_initialization(
            db,
            config,
            registry,
            vm_template,
            admin,
            vm_name,
            ts_target,
            providers,
            home,
            admin_username,
            logger,
            git_tokens=git_tokens,
            is_first_init=True,
        )


def run_initialization(
    db: Database,
    config: Config,
    registry: Registry,
    vm_template: ResolvedVMTemplate,
    admin: AdminConfig,
    vm_name: str,
    ts_target: Transport,
    providers: dict[str, GitCredentialProvider],
    home: str,
    admin_username: str,
    logger: SSHLogger,
    *,
    git_tokens: dict[str, str],
    is_first_init: bool = False,
) -> None:
    """Run Phase B (initialization) with status tracking and event logging.

    This is called both from initialize_vm() after provisioning and
    from reinit_vm() for repeatable re-initialization. Pass
    ``is_first_init=True`` from initialize_vm so steps that expect prior
    state (e.g. tmux socket dirs) can skip warnings on missing state.
    ``git_tokens`` is required (no provider-side fallback);
    callers must thread the framework-resolved dict in.
    """
    db.insert_vm_event(vm_name, "init_started")

    try:
        _phase_b_setup(
            db,
            config,
            registry,
            vm_template,
            admin,
            vm_name,
            ts_target,
            providers,
            home,
            admin_username,
            logger,
            git_tokens=git_tokens,
            is_first_init=is_first_init,
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
    vm_template: ResolvedVMTemplate,
    vm_name: str,
    exec_target: Transport,
    home: str,
    admin_username: str,
    hostname: str,
    logger: SSHLogger,
    *,
    tailscale_auth_key: str,
    script_swap: int,
    bootstrap_complete: bool = False,
    tailscale_ip: str | None = None,
) -> Transport:
    """Phase A: Bootstrap (over provisioning transport). All steps are fatal.

    Three paths depending on how much the platform already handled:

    1. bootstrap_complete=True (Lima/Azure): The platform already ran the
       full bootstrap. Skip straight to Tailscale SSH verification.
    2. Otherwise (WSL2): Run full bootstrap script over the provisioning
       transport (user, packages, SSH key, swap, Tailscale).

    Returns the Tailscale ``Transport`` for Phase B.
    """
    db.update_vm_provisioning_status(vm_name, ProvisioningStatus.IN_PROGRESS)

    if bootstrap_complete and tailscale_ip:
        # Lima/Azure: platform already ran the full bootstrap.
        # Just update DB and move on to SSH verification.
        logger.step("Bootstrap (platform)")
        logger.output(f"Tailscale IP: {tailscale_ip}")
        db.update_vm_tailscale(vm_name, tailscale_ip)
        db.update_vm_provisioning_status(vm_name, ProvisioningStatus.COMPLETE)
    else:
        # WSL2: run bootstrap script over the provisioning transport
        tailscale_ip = _run_bootstrap_script(
            db,
            config,
            vm_template,
            vm_name,
            exec_target,
            admin_username,
            hostname,
            logger,
            tailscale_auth_key=tailscale_auth_key,
            script_swap=script_swap,
        )

    # Sync the operator's SSH config now that the VM's Tailscale IP is
    # known. Phase B issues many SSH calls; having the managed aliases in
    # place first means operator-facing ``ssh awvm--<name>`` works as soon
    # as the VM is reachable.
    from agentworks.ssh_config import sync_ssh_config

    sync_ssh_config(config, db)

    # Switch to Tailscale SSH, carrying over the SSH logger.
    # On Windows, force TTY to prevent zsh/login shell pipe hangs.
    import sys

    ts_target = SSHTransport(
        host=tailscale_ip,
        user=admin_username,
        identity_file=config.operator.ssh_private_key,
        force_tty=sys.platform == "win32",
        default_timeout=60,
        logger=logger,
    )

    # Verify Tailscale SSH works (retry -- peer connection may take time)
    logger.step("Verify Tailscale SSH")
    output.detail("Verifying Tailscale SSH...")
    import time

    for attempt in range(5):
        try:
            ts_target.run("echo ok", timeout=15)
            break
        except SSHError:
            if attempt == 4:
                raise
            output.detail(f"Tailscale SSH not ready, retrying ({attempt + 1}/5)...")
            time.sleep(3)

    return ts_target


def _run_bootstrap_script(
    db: Database,
    config: Config,
    vm_template: ResolvedVMTemplate,
    vm_name: str,
    exec_target: Transport,
    admin_username: str,
    hostname: str,
    logger: SSHLogger,
    *,
    tailscale_auth_key: str,
    script_swap: int,
) -> str:
    """Generate, copy, and run a bootstrap script on the VM. Returns Tailscale IP.

    Used for WSL2 where the bootstrap cannot be embedded in a platform's
    native mechanism (Lima provision block, Azure cloud-init).
    ``tailscale_auth_key`` is required; the framework-resolved value
    arrives from ``create_vm`` -> ``initialize_vm`` -> ``_phase_a_bootstrap``.
    """
    import tempfile

    from agentworks.capabilities.vm_platform.bootstrap_script import (
        generate_bootstrap_script,
        parse_bootstrap_output,
    )

    output.info("Bootstrapping VM...")

    ssh_public_key = config.operator.ssh_public_key.read_text().strip()
    script = generate_bootstrap_script(
        admin_username=admin_username,
        ssh_public_key=ssh_public_key,
        provisioning_packages=PROVISIONING_PACKAGES,
        tailscale_auth_key=tailscale_auth_key,
        # The stored hostname (vms.hostname), never re-derived from
        # live config.
        hostname=hostname,
        swap=script_swap,
    )

    # Copy script to VM and execute synchronously over the provisioning transport
    remote_script = "/tmp/agentworks-bootstrap.sh"
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".sh", delete=False) as f:
        f.write(script.encode("utf-8"))
        local_script = f.name

    try:
        exec_target.copy_to(local_script, remote_script)
    finally:
        import os

        os.unlink(local_script)

    # Run the bootstrap script synchronously over the platform's provisioning
    # transport. WSL2 is the only consumer here (Lima/Azure embed the bootstrap
    # in their native delivery mechanisms and arrive with bootstrap_complete=True),
    # and the WSL2 transport is a local wsl.exe subprocess -- there is no
    # network session to disconnect from, so the detached-poll pattern brings
    # no benefit. It also actively breaks WSL2 under systemd: each `wsl.exe`
    # invocation is its own systemd-logind user session, and the default
    # KillUserProcesses=yes reaps every process in the session cgroup when the
    # foreground shell exits -- nohup blocks SIGHUP, not cgroup teardown.
    #
    # The wrapping bits (in order) defend against terminal-related hangs:
    #   setsid       - new session, no controlling TTY. Without this, sudo's
    #                  default `Defaults use_pty` allocates a pty whose
    #                  foreground PGID is sudo's monitor; any dpkg trigger
    #                  that touches /dev/tty from a background PGID then
    #                  SIGTTIN/SIGTTOU-stops apt mid-`dist-upgrade`.
    #   </dev/null   - stdin = EOF, so anything that reads from stdin (rather
    #                  than /dev/tty directly) returns immediately.
    #   2>&1         - merge stderr into captured stdout so apt-get noise
    #                  lands alongside the script's ##STEP## markers when
    #                  we need to diagnose a failure.
    output.detail("Running bootstrap script...")
    result = exec_target.run(
        f"setsid sudo -n /bin/bash {remote_script} </dev/null 2>&1",
        check=False,
        timeout=900,  # 15 min hard cap; apt-get dist-upgrade is the long pole
    )
    exec_target.run(f"rm -f {remote_script}", sudo=True, check=False)

    # Parse structured output
    bootstrap = parse_bootstrap_output(result.stdout, result.returncode)

    # Feed results into logger and console
    for step in bootstrap.steps:
        logger.step(step.name)
        if step.success_msg:
            output.detail(f"{step.name}: {step.success_msg}")
            logger.output(step.success_msg)
        for warning in step.warnings:
            output.warn(warning)
            logger.warning(warning)
        if step.error:
            output.warn(f"Error: {step.error}")
            logger.log_error(step.error)

    # Log full output for troubleshooting
    if result.stdout:
        logger.output(result.stdout)

    if not bootstrap.ok:
        msg = f"Bootstrap script failed (exit {result.returncode})"
        if result.stdout:
            msg += f"\n{result.stdout[-500:]}"
        raise SSHError(msg)

    # Update DB with Tailscale info
    assert bootstrap.tailscale_ip is not None
    tailscale_ip = bootstrap.tailscale_ip
    output.detail(f"Tailscale IP: {tailscale_ip}")
    db.update_vm_tailscale(vm_name, tailscale_ip)
    db.update_vm_provisioning_status(vm_name, ProvisioningStatus.COMPLETE)

    return tailscale_ip


def _phase_b_setup(
    db: Database,
    config: Config,
    registry: Registry,
    vm_template: ResolvedVMTemplate,
    admin: AdminConfig,
    vm_name: str,
    ts_target: Transport,
    providers: dict[str, GitCredentialProvider],
    home: str,
    admin_username: str,
    logger: SSHLogger,
    *,
    git_tokens: dict[str, str],
    is_first_init: bool = False,
) -> None:
    """Phase B: Setup (over Tailscale SSH). Non-fatal steps warn and continue.

    ``git_tokens`` is required: every provider listed in
    ``providers`` must have a pre-resolved token value in the dict.
    """
    with output.section("VM Initialization"):
        from agentworks.resources.access import kind_dict

        output.info(f"vm: {vm_name}")
        db.update_vm_init_status(vm_name, InitStatus.IN_PROGRESS)
        # Reference validation lives in the framework (the apt / install-command
        # kinds' error miss policy fires at build_registry time, which the
        # manager-entry hoist runs before reaching this point). Read the kinds
        # this phase drives directly from the finalized registry.
        apt_sources = kind_dict(registry, "apt-source")
        apt_packages = kind_dict(registry, "apt-package")
        system_install_commands = kind_dict(registry, "system-install-command")
        user_install_commands = kind_dict(registry, "user-install-command")

        # Non-fatal: ensure cloud-init won't regenerate SSH host keys on reboot.
        # Runs first so VMs predating the Phase A step are repaired on reinit
        # even if a later step warns. Idempotent overwrite with identical
        # content.
        _preserve_ssh_host_keys(ts_target, logger)

        # Non-fatal: repair the Apple-vz SVE trap (arm64.nosve grub drop-in) on
        # VMs provisioned before the Phase A mask existed. Runs early, before the
        # crypto-dependent apt/source steps, so a broken VM at least gets the fix
        # installed this pass; it needs a restart plus one more reinit to converge.
        # A silent no-op on every non-Apple host and on already-masked VMs.
        _apply_sve_mask(ts_target, logger)

        # Non-fatal: VM hardening (sysctl baseline + /proc hidepid>=1).
        # Runs before the rest of init so subsequent steps execute under the
        # hardened baseline. Depends only on coreutils + procps (always
        # present); nothing here needs apt-installed packages. Idempotent on
        # reinit.
        from agentworks.vms.hardening import apply_vm_hardening

        apply_vm_hardening(ts_target, logger)

        # Check VM DNS works before subsequent steps that need external
        # resolution (apt-get update, source fetches, etc.) fail cryptically.
        # When DNS is broken AND the failure matches the known issue #117
        # latched shape AND the heal applies to this resolver setup, raises
        # StateError with the manual heal block as a hint. When DNS is broken
        # for any other reason, surfaces a non-fatal warning so the operator
        # has a visible link to the apt failure that will follow.
        from agentworks.vms.tailscale_dns import (
            apply_tailscaled_dns_fix,
            check_vm_dns,
        )

        check_vm_dns(ts_target, logger)

        # Non-fatal: tailscaled cold-boot DNS race fix (GitHub issue #117).
        # Drops in a systemd override that orders tailscaled after the DNS
        # layer is up so its DNS-manager probe finds a resolver instead of
        # falling back to direct mode. Applied early in Phase B so existing
        # VMs pick up the fix on the first reinit. Does not restart
        # tailscaled (would disconnect us); takes effect on next cold boot.
        apply_tailscaled_dns_fix(ts_target, logger)

        # Non-fatal: VM-wide SetEnv plumbing.
        # Runs before apt install so subsequent SSH commands within init can
        # rely on the SetEnv path. These targets don't touch zsh-shipped files,
        # so dpkg conffile handling doesn't apply.
        _write_sshd_accept_env(ts_target, logger)
        _write_sudoers_env_keep(ts_target, logger)
        # Pairs with the --preserve-env in _split_shell_pane's agent-pane branch.
        _write_sudoers_console_setenv(ts_target, logger, admin_username)
        vm_row = db.get_vm(vm_name)
        # Init runs against a VM that exists in the DB (initialize_vm fetches the
        # row up front). A None here is an internal invariant violation, not a
        # recoverable state, so surface it loudly.
        assert vm_row is not None, f"VM '{vm_name}' missing from DB mid-init"
        # The platform name resolves through the site declaration at this
        # composition root (a stranded remote-Lima VM already failed reinit
        # at the earlier bind, before any env baking).
        from agentworks.vms.sites import site_platform_name

        identity_ctx = ResourceContext(
            vm_name=vm_row.name,
            platform=site_platform_name(vm_row.site, registry),
            site=vm_row.site,
            user=admin_username,
        )

        # Provisioning is hermetic: no operator env, no per-context identity,
        # no secrets from env tables are injected into install commands. Static
        # identity (AGENTWORKS_VM / SITE / PLATFORM) reaches install commands
        # via /etc/profile.d/agentworks-identity.sh sourcing. Tailscale auth key
        # and git credentials -- the only provisioning-time secrets -- have
        # their own dedicated config paths outside [admin.env]. Operator env
        # only reaches RUNTIME shells (vm shell, agent shell, sessions,
        # consoles), never build-time install machinery.

        # Non-fatal: system repos + packages (mise repo added, then all packages)
        _install_system_packages(ts_target, logger)

        # Non-fatal: apt sources required by selected apt_packages
        _configure_apt_sources(ts_target, vm_template, apt_packages, apt_sources, logger)

        # Non-fatal: apt packages (direct list + apt-package entries)
        _install_apt_packages(ts_target, vm_template, apt_packages, logger)

        # Identity profile fragments. Runs AFTER apt install because apt uses
        # `--force-confnew`, which would replace the agentworks block in
        # `/etc/zsh/zprofile` with zsh-common's package default if zsh got
        # installed after we wrote our fragment. Post-install, we append cleanly
        # on top of whatever the package shipped. The mirror is idempotent on
        # reinit (strip-and-rewrite via begin/end markers).
        _write_agentworks_identity_profile(
            ts_target,
            vm_stable_identity_env(identity_ctx),
            logger,
        )

        # /etc/skel seeds. MUST run AFTER apt for the same reason as the
        # identity profile above: `/etc/skel/.bashrc` is a Debian conffile
        # shipped by the `bash` package. Running before apt's
        # `--force-confnew` would let a bash upgrade silently replace the
        # seed with Debian's stock skel (saving ours as .dpkg-old). Future
        # `useradd -m` would then inherit Debian's skel instead.
        _write_skel_seeds(ts_target, logger)

        # Non-fatal: snap packages
        if vm_template.snap:
            logger.step("Snap packages")
            output.info(f"Installing {output.count(len(vm_template.snap), 'snap package')}...")
            for pkg in vm_template.snap:
                try:
                    ts_target.run(f"snap install {shlex.quote(pkg)}", sudo=True, timeout=120)
                except SSHError as e:
                    msg = f"snap install '{pkg}' failed: {e}"
                    logger.warning(msg)
                    output.warn(msg)

        # admin_shell is a pure config read, hoisted above the system install
        # commands below (which run in it) so they stay in the VM section; the
        # login-shell usermod is a separate admin step further down.
        admin_shell = admin.shell

        # Non-fatal: system install commands (VM-level, system-wide). Kept in
        # the VM section: they run via ``{admin_shell} -lc`` explicitly, so
        # they do not depend on the login-shell usermod, and they install
        # system-wide tools rather than touching the admin's rc.
        system_path = _run_install_commands(
            ts_target,
            vm_template.system_install_commands,
            system_install_commands,
            admin_shell,
            home,
            logger,
            label="System install command",
        )

        # Non-fatal: agent tmux socket directory infrastructure (VM-level:
        # shared group, root directory, per-agent subdirectories, all
        # root-owned system state). No dependency on the admin steps below, so
        # it closes out the VM phase.
        try:
            from agentworks.sessions.tmux import (
                cleanup_stale_sockets,
                ensure_agent_socket_dir,
                ensure_agent_socket_root,
            )

            logger.step("Agent tmux socket directories")
            output.info("Setting up agent tmux socket infrastructure...")

            ensure_agent_socket_root(ts_target, admin_username, warn_if_missing=not is_first_init)
            for agent in db.list_agents(vm_name=vm_name):
                ensure_agent_socket_dir(ts_target, agent.linux_user)
                removed = cleanup_stale_sockets(ts_target, agent.linux_user)
                if removed:
                    output.detail(f"Cleaned up {output.count(removed, 'stale socket')} for {agent.linux_user}")
        except SSHError as e:
            msg = f"agent tmux socket setup failed: {e}"
            logger.warning(msg)
            output.warn(msg)

    with output.section("Admin Initialization"):
        # Non-fatal: set default shell (before the USER install commands so
        # those installers write to the correct rc file). The zsh
        # ``zsh-newuser-install`` first-run wizard is pre-empted by the skel seed.
        logger.step("Shell configuration")
        output.info(f"Setting shell to {admin_shell}...")
        try:
            ts_target.run(
                f"usermod -s $(which {shlex.quote(admin_shell)}) {shlex.quote(admin_username)}",
                sudo=True,
            )
        except SSHError as e:
            msg = f"shell configuration failed: {e}"
            logger.warning(msg)
            output.warn(msg)

        # Non-fatal: reconcile authorized_keys
        _reconcile_authorized_keys(ts_target, config, home, logger)

        # Non-fatal: workspaces directory with ACLs for group-writable files.
        # Default ACLs ensure new files/dirs inherit group rwx regardless of umask.
        # Access ACLs fix existing files. Applied recursively to cover all workspaces.
        workspaces_dir = config.paths.vm_workspaces
        if workspaces_dir.startswith("/home/"):
            output.warn(
                f"vm_workspaces is under /home ({workspaces_dir}). "
                "This may require the home directory to be world-traversable."
            )
        try:
            # acl is now installed as a system package in _install_system_packages
            ts_target.run(f"mkdir -p {workspaces_dir}", sudo=True)
            # Ensure all parent directories are traversable by agents
            ts_target.run(
                f'sh -c \'p={workspaces_dir}; while [ "$p" != "/" ]; do chmod a+x "$p"; p=$(dirname "$p"); done\'',
                sudo=True,
            )
            # Default ACLs on directories only (setfacl -R -d warns on files)
            ts_target.run(
                f"find {workspaces_dir} -type d -exec setfacl -d -m g::rwx -m m::rwx {{}} +",
                sudo=True,
                timeout=120,
            )
            # Access ACLs on all existing files and dirs
            ts_target.run(
                f"setfacl -R -m g::rwx -m m::rwx {workspaces_dir}",
                sudo=True,
                timeout=120,
            )
        except SSHError as e:
            msg = f"workspaces directory setup failed: {e}"
            logger.warning(msg)
            output.warn(msg)

        # Non-fatal: mise config (written before dotfiles so dotfiles can override)
        mise_path: list[str] = _mise_shims_path(home)
        if admin.mise_packages:
            _write_mise_config(ts_target, admin.mise_packages, admin.mise_install_before, home, logger)

        # Non-fatal: git safe.directory wildcard (disables ownership checks for the
        # multi-user workspace model where agents access repos owned by admin)
        if admin.git_force_safe_directory:
            try:
                ts_target.run("git config --global --add safe.directory '*'")
                output.info("Git safe.directory wildcard configured")
            except SSHError as e:
                msg = f"git safe.directory setup failed: {e}"
                logger.warning(msg)
                output.warn(msg)

        # Non-fatal: git credentials (before dotfiles and mise lockfile for private repos)
        if providers:
            _configure_git_credentials(vm_name, ts_target, providers, logger, git_tokens=git_tokens, config=config)

        # Non-fatal: dotfiles (can override mise config, can provide lockfile)
        if admin.dotfiles_source:
            logger.step("Dotfiles")
            dest = admin.dotfiles_destination.replace("~", home)
            try:
                from agentworks.sources import SourceRefError, fetch_dir, parse_source_ref

                ref = parse_source_ref(admin.dotfiles_source)
                output.info(f"Syncing dotfiles from {admin.dotfiles_source}...")
                fetch_dir(ref, ts_target, dest, logger=logger)

                output.info(f"Running dotfiles install: {admin.dotfiles_install_cmd}")
                ts_target.run(
                    f"cd {dest} && {admin.dotfiles_install_cmd}",
                    timeout=120,
                )
            except (SourceRefError, Exception) as e:
                msg = f"dotfiles install failed: {e}"
                logger.warning(msg)
                output.warn(msg)

        # Non-fatal: mise lockfile (after git creds and dotfiles; overrides dotfiles lockfile)
        if admin.mise_lockfile:
            _fetch_mise_lockfile(ts_target, admin.mise_lockfile, home, logger)

        # Non-fatal: mise install (after config + dotfiles + lockfile are all settled)
        prune = admin.mise_prune_on_reinit
        if admin.mise_packages or admin.mise_lockfile:
            _run_mise_install(
                ts_target,
                admin_shell,
                home,
                admin.mise_allow_unlocked,
                logger,
                prune=prune,
            )
        else:
            try:
                check = ts_target.run(f"test -f {home}/.config/mise/config.toml", check=False)
                if check.ok:
                    _run_mise_install(
                        ts_target,
                        admin_shell,
                        home,
                        admin.mise_allow_unlocked,
                        logger,
                        prune=prune,
                    )
            except SSHError:
                pass

        # Non-fatal: user install commands for admin user (may depend on mise tools)
        user_path = _run_install_commands(
            ts_target,
            admin.user_install_commands,
            user_install_commands,
            admin_shell,
            home,
            logger,
            label="User install command",
        )

        # Non-fatal: shell profile (PATH exports sourced at login)
        all_paths = system_path + mise_path + user_path
        _write_agentworks_profile(ts_target, all_paths, logger)

        # Non-fatal: shell rc (interactive shell hooks like mise activate)
        rc_snippets = [MISE_ACTIVATE_LINES] if admin.mise_activate else ["# mise activation disabled"]
        _write_agentworks_rc(ts_target, rc_snippets, logger)

        # Non-fatal: Claude Code marketplaces and plugins for admin user
        def _admin_run_cmd(cmd: str, timeout: int) -> object:
            inner = shlex.quote(cmd)
            return ts_target.run(f"{admin_shell} -lc {inner}", timeout=timeout)

        install_claude_plugins(_admin_run_cmd, admin.claude_marketplaces, admin.claude_plugins, logger)

        # Defensive final step: re-ensure source lines in case any earlier
        # step (dotfiles install in particular) overwrote a shell rc file
        # in place. Idempotent grep-or-append.
        _ensure_agentworks_files_sourced(
            ts_target,
            home=home,
            shell=admin_shell,
            logger=logger,
        )


RunCmd = Callable[[str, int], object]
"""Callable that runs a shell command with a timeout. Used to abstract
the choice of ``Transport`` (admin vs agent) at the call site."""


def install_claude_plugins(
    run_cmd: RunCmd,
    marketplaces: list[str],
    plugins: list[str],
    logger: SSHLogger | None = None,
) -> None:
    """Register Claude Code marketplaces and install plugins. Non-fatal.

    The caller provides a ``run_cmd`` that wraps the command in a login
    shell (``{shell} -lc <cmd>``) so the calling user's PATH (mise shims,
    ``~/.local/bin``, etc.) is in scope. A plain non-interactive SSH
    invocation gets a non-login shell that sources neither ``.bashrc``
    nor ``.profile``, so ``command -v claude`` would falsely fail. Both
    the admin call site (``_phase_b_setup`` in this file) and the agent
    call site (``create_agent_on_vm`` in ``agents/initializer.py``) wrap
    accordingly; the helper itself stays transport- and user-agnostic.
    """
    if not marketplaces and not plugins:
        return

    if logger:
        logger.step("Claude plugins")

    try:
        # Verify claude is available before attempting marketplace/plugin setup
        run_cmd("command -v claude >/dev/null 2>&1", 10)
    except SSHError as e:
        msg = (
            f"claude CLI not available; skipping marketplace/plugin setup ({e}). "
            "Install claude (e.g. via user_install_commands or any other method) and rerun init."
        )
        if logger:
            logger.warning(msg)
        output.warn(msg)
        return

    try:
        for source in marketplaces:
            output.info(f"Registering Claude marketplace: {source}")
            run_cmd(f"claude plugin marketplace add {shlex.quote(source)}", 60)

        for plugin in plugins:
            output.info(f"Installing Claude plugin: {plugin}")
            run_cmd(f"claude plugin install {shlex.quote(plugin)} --scope user", 60)
    except SSHError as e:
        msg = f"Claude plugin install failed: {e}"
        if logger:
            logger.warning(msg)
        output.warn(msg)
