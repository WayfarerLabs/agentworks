"""The two-phase init driver: Phase A (record and verify, over the provisioning
transport) and Phase B (setup, over Tailscale SSH,
non-fatal-on-failure).

Phase A is provisioning: after the platform completes create-time bootstrap,
``bootstrap_vm`` records the returned or rediscovered Tailscale IP, verifies
Tailscale SSH, and syncs SSH config for a freshly provisioned VM. It returns
the Tailscale transport and home for Phase B. Phase B is
initialization: ``run_initialization`` runs it, both after ``bootstrap_vm``
on ``vm create`` and standalone on ``vm reinit``.

``create_vm`` (in ``vms.manager.lifecycle``) drives ``bootstrap_vm`` inside
its ``Provisioning`` output section and ``run_initialization`` after it, so
the phase boundary matches the section boundary; it also owns the keepalive
hold spanning both phases and the create-vs-init error mapping. This driver
owns only the per-phase step sequences and status/event bookkeeping.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.db import InitStatus, ProvisioningStatus
from agentworks.env import ResourceContext, vm_stable_identity_env
from agentworks.git_config import ensure_safe_directory_wildcard
from agentworks.git_credentials import configure_user_git_credentials
from agentworks.ssh import SSHError, SSHLogger
from agentworks.transports import SSHTransport, Transport

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
    _resolve_apt_sources,
    _run_install_commands,
)
from .shell_env import (
    _ensure_agentworks_files_sourced,
    _harden_admin_home,
    _write_agentworks_identity_profile,
    _write_agentworks_profile,
    _write_agentworks_rc,
    _write_skel_seeds,
    _write_sshd_accept_env,
    _write_sudoers_console_setenv,
    _write_sudoers_env_keep,
)
from .ssh_keys import (
    AuthorizedKeysApplied,
    AuthorizedKeysOutcome,
    AuthorizedKeysUnproven,
    _apply_sve_mask,
    _preserve_ssh_host_keys,
    _reconcile_authorized_keys,
)
from .workspaces_dir import _setup_workspaces_directory

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.debian import DebianRelease
    from agentworks.git_credentials import CredentialRequest
    from agentworks.resources.registry import Registry
    from agentworks.vms.admin import AdminConfig
    from agentworks.vms.templates import ResolvedVMTemplate


class VMInitializationOperation(StrEnum):
    """Closed lifecycle operations that can establish VM applied state."""

    VM_CREATE = "vm-create"
    VM_REINIT = "vm-reinit"


def bootstrap_vm(
    db: Database,
    config: Config,
    vm_name: str,
    exec_target: Transport,
    platform: VMPlatform,
    ctx: RunContext,
    logger: SSHLogger,
    *,
    admin_username: str,
    tailscale_ip: str | None = None,
    on_tailscale_ready: Callable[[], None] | None = None,
) -> tuple[Transport, str]:
    """Run Phase A provisioning state recording and connectivity verification.

    Runs over the provisioning transport after platform-owned bootstrap:
    optional Tailscale IP rediscovery, Tailscale-SSH verification, the
    post-Tailscale-ready hook plus reconnect wait, and finally the SSH-config
    sync before the caller's explicit provisioning-complete line. Only the
    IP discovery or verification is fatal to provisioning: a failure there means the VM is
    unreachable, so it marks provisioning ``failed``, records the
    ``provisioning_failed`` event, secures the kept VM via the platform's
    best-effort ``secure_failed_vm`` hook (Azure deletes its ephemeral
    bootstrap SSH allow so a failed VM defaults to zero inbound
    exposure), points at the manager-owned log, and re-raises for
    ``create_vm`` to map to a ProvisioningError (delete guidance). An
    operator interrupt (KeyboardInterrupt) escapes that Exception arm but
    still secures the kept VM best-effort before propagating; the row's
    status is left as the abort found it (the caller's cancel handler
    owns the messaging). The
    connectivity cleanup and the SSH-config sync are non-fatal: they cannot
    make an already-bootstrapped VM unhealthy, so a failure there warns and
    continues into Phase B rather than stranding a reachable VM as FAILED.

    Returns ``(ts_target, home)`` for Phase B (``run_initialization``).
    The caller owns the keepalive hold spanning both phases (this function
    does not open it) and the ``Provisioning`` output section (this function
    emits into the ambient section, opening none of its own). The manager
    constructs and closes ``logger``; this function only writes Phase A work
    to it.

    ``ctx`` is the create op's own scoped :class:`RunContext`, threaded
    in so the ``secure_failed_vm`` hook (a backend NSG call on Azure)
    reads its credential from it with no ambient fallback. It carries
    already-resolved secrets: ``create_vm``'s boundary resolve ran, and
    the ``platform_ctx`` it built, before this function is called, so
    even the interrupt arm below never resolves a secret for the first
    time.
    """
    home = f"/home/{admin_username}"

    # Attach logger to the provisioning transport. ``Transport`` declares
    # ``logger`` on the ABC; the assignment is polymorphic.
    exec_target.logger = logger

    transport = exec_target.describe()

    vm_row = db.get_vm(vm_name)
    assert vm_row is not None, "create_vm inserts the row before init"
    # The fatal span is only IP discovery and Tailscale SSH verification. A
    # failure means the VM is unreachable, so it is marked provisioning ``failed`` and
    # routed to delete. The connectivity cleanup and the SSH-config sync below
    # sit OUTSIDE this span deliberately: they cannot make an already-bootstrapped
    # VM unhealthy, so a failure there must not flip a reachable VM to FAILED
    # (which would foreclose ``vm reinit``, since reinit requires COMPLETE).
    try:
        db.insert_vm_event(vm_name, "provisioning_started", transport)
        ts_target = _phase_a_bootstrap(
            db,
            config,
            vm_name,
            exec_target,
            admin_username,
            logger,
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
        # Fail closed: the VM is kept for debugging, so close its
        # provisioning access now (Azure deletes the ephemeral bootstrap
        # SSH allow; other platforms no-op). The success-path hook
        # (on_tailscale_ready) never fired here, so without this a
        # failed Azure VM would keep its bootstrap ingress indefinitely.
        # Best-effort: the original failure must keep propagating, and
        # the operator can still reach the VM via `vm shell --platform`
        # (a fresh transient allow) or the platform's serial console
        # (not NSG-gated).
        try:
            platform.secure_failed_vm(vm_row, ctx)
        except Exception as secure_error:
            output.warn(f"could not secure the failed VM: {secure_error}")
        output.warn(f"Log: {logger.display_path}")
        raise
    except BaseException:
        # An operator interrupt (KeyboardInterrupt) or another
        # non-Exception unwind during connectivity verification. The
        # row keeps whatever status the abort left (the caller's cancel
        # handler owns the messaging; nothing here marks FAILED), but
        # provisioning access must still close best-effort: without
        # this, Ctrl-C would leave the Azure bootstrap allow standing
        # indefinitely, since the Exception arm above never runs.
        # UserAbort does NOT take this path (it is an AgentworksError,
        # an Exception, so the arm above secures it); this arm exists
        # for what genuinely escapes ``except Exception``. The hook
        # failure warns and never masks the interrupt.
        try:
            platform.secure_failed_vm(vm_row, ctx)
        except Exception as secure_error:
            output.warn(f"could not secure the interrupted VM: {secure_error}")
        raise

    # Tailscale is up; the platform hook closes provisioning access
    # (Azure deletes its ephemeral bootstrap SSH allow since Phase B
    # uses Tailscale SSH). A route change can destabilize the network
    # stack briefly, so we wait for Tailscale SSH to be reliably
    # reachable before proceeding. Already non-fatal (its own try / a
    # bounded wait); kept outside the FAILED-marking span above, as in
    # the pre-split driver.
    if on_tailscale_ready is not None:
        try:
            on_tailscale_ready()
        except Exception as e:
            output.warn(f"post-Tailscale-ready hook failed: {e}")

        # Wait for Tailscale SSH to reconnect after network changes
        from agentworks.transports import wait_for_reconnect

        wait_for_reconnect(ts_target)

    # Sync the operator's SSH config now that connectivity is verified. This is
    # the last step of Phase A before the caller announces completion: the VM's
    # Tailscale IP is recorded and the connection is
    # confirmed, so operator-facing ``ssh awvm--<name>`` aliases are in place
    # before Phase B's many SSH calls. Non-fatal: a local ``~/.ssh/config``
    # write failure (permissions, read-only home) has nothing to do with VM
    # health, so it warns and continues into Phase B rather than failing the
    # create. Matches the caller's post-init re-sync handling.
    try:
        from agentworks.ssh_config import sync_ssh_config

        sync_ssh_config(config, db)
    except Exception as e:
        output.warn(f"SSH config sync failed: {e}")
        output.info("VM is likely still usable.")

    return ts_target, home


def run_initialization(
    db: Database,
    config: Config,
    registry: Registry,
    vm_template: ResolvedVMTemplate,
    admin: AdminConfig,
    vm_name: str,
    ts_target: Transport,
    credential_requests: tuple[CredentialRequest, ...],
    home: str,
    admin_username: str,
    logger: SSHLogger,
    *,
    debian_release: DebianRelease,
    operation: VMInitializationOperation,
) -> None:
    """Run Phase B (initialization) with status tracking and event logging.

    This is called both from ``create_vm`` (after ``bootstrap_vm``) and
    from ``reinit_vm`` for repeatable re-initialization. The closed
    ``operation`` value also drives create-only setup behavior.
    Each credential request carries the provider's scoped context assembler.
    """
    db.insert_vm_event(vm_name, "init_started")

    try:
        authorized_keys = _phase_b_setup(
            db,
            config,
            registry,
            vm_template,
            admin,
            vm_name,
            ts_target,
            credential_requests,
            home,
            admin_username,
            logger,
            debian_release=debian_release,
            operation=operation,
        )
    except Exception as e:
        with db.transaction():
            db.update_vm_init_status(vm_name, InitStatus.FAILED)
            db.insert_vm_event(vm_name, "init_failed", str(e))
        raise

    if isinstance(authorized_keys, AuthorizedKeysUnproven):
        output.warn(
            f"SSH identity evidence for VM '{vm_name}' is unknown, so ordinary SSH commands "
            "will refuse to connect. If the configured key still works, retry with "
            f"'agw vm reinit {vm_name}'. Otherwise, use "
            f"'agw vm shell {vm_name} --platform' where supported to restore access before "
            "reinitializing. If platform recovery is unavailable, recreate the VM."
        )

    from agentworks.db.instance_state import AppliedStateKey
    from agentworks.vms.applied_state import build_vm_initialization_slices

    applied_proof = authorized_keys if isinstance(authorized_keys, AuthorizedKeysApplied) else None
    slices = build_vm_initialization_slices(
        applied_proof,
        include_hardware=operation is VMInitializationOperation.VM_CREATE,
    )
    ssh_identity_proven = AppliedStateKey.SSH_IDENTITY in slices
    if applied_proof is not None and not ssh_identity_proven:
        # File-system identity cannot be checkpointed atomically with the
        # remote write. A changed or unreadable carrier leaves the remote
        # result unknown, so remove any older proof.
        msg = "configured SSH identity changed or became unavailable after authorized_keys update"
        logger.warning(msg)
        output.warn(msg)

    final_status = InitStatus.PARTIAL if logger.has_warnings else InitStatus.COMPLETE
    final_event = "init_partial" if logger.has_warnings else "init_complete"
    final_detail = f"{len(logger.warnings)} warning(s)" if logger.has_warnings else None

    # Do not include this checkpoint in the Phase B exception arm above: a
    # local database failure must roll back the complete terminal result and
    # propagate, leaving the earlier in-progress evidence intact.
    with db.transaction():
        db.update_vm_init_status(vm_name, final_status)
        db.insert_vm_event(vm_name, final_event, final_detail)
        if slices:
            db.instance_state.replace_applied_slices(
                "vm",
                vm_name,
                operation.value,
                slices,
            )
        if not ssh_identity_proven:
            db.instance_state.clear_applied_slice(
                "vm",
                vm_name,
                AppliedStateKey.SSH_IDENTITY,
            )


def _phase_a_bootstrap(
    db: Database,
    config: Config,
    vm_name: str,
    exec_target: Transport,
    admin_username: str,
    logger: SSHLogger,
    *,
    tailscale_ip: str | None = None,
) -> Transport:
    """Phase A: record platform bootstrap and verify Tailscale SSH.

    ``VMPlatform.create`` already completed bootstrap. If it could not
    discover the joined node's IP, repeat only ``tailscale ip -4`` over the
    returned provisioning transport. No bootstrap input or credential reaches
    this function.

    Returns the Tailscale ``Transport`` for Phase B.
    """
    db.update_vm_provisioning_status(vm_name, ProvisioningStatus.IN_PROGRESS)

    logger.step("Bootstrap (platform)")
    if not tailscale_ip:
        logger.output("Tailscale joined; retrying IP discovery")
        tailscale_ip = exec_target.run("tailscale ip -4", sudo=True).stdout.strip()
    logger.output(f"Tailscale IP: {tailscale_ip}")
    db.update_vm_tailscale(vm_name, tailscale_ip)
    db.update_vm_provisioning_status(vm_name, ProvisioningStatus.COMPLETE)

    # Switch to Tailscale SSH, carrying over the SSH logger.
    # On Windows, force TTY to prevent zsh/login shell pipe hangs.
    ts_target = SSHTransport(
        host=tailscale_ip,
        user=admin_username,
        identity_file=config.operator.ssh_private_key,
        force_tty=sys.platform == "win32",
        default_timeout=60,
        logger=logger,
    )

    # Verify Tailscale SSH works (retry: peer connection may take time)
    logger.step("Verify Tailscale SSH")
    output.info("Verifying Tailscale SSH...")
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


def _phase_b_setup(
    db: Database,
    config: Config,
    registry: Registry,
    vm_template: ResolvedVMTemplate,
    admin: AdminConfig,
    vm_name: str,
    ts_target: Transport,
    credential_requests: tuple[CredentialRequest, ...],
    home: str,
    admin_username: str,
    logger: SSHLogger,
    *,
    debian_release: DebianRelease,
    operation: VMInitializationOperation,
) -> AuthorizedKeysOutcome:
    """Phase B: Setup (over Tailscale SSH). Non-fatal steps warn and continue."""
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

        # Resolve every selected release map before Phase B touches the
        # guest. A missing mapping is a configuration boundary, not a
        # partial initialization after unrelated convergence already ran.
        resolved_apt_sources = _resolve_apt_sources(
            vm_template,
            apt_packages,
            apt_sources,
            debian_release=debian_release,
        )

        # Non-fatal: ensure cloud-init won't regenerate SSH host keys on reboot.
        # Runs first so VMs predating the create-time bootstrap step are
        # repaired on reinit even if a later step warns. Idempotent overwrite
        # with identical content.
        _preserve_ssh_host_keys(ts_target, logger)

        # Non-fatal: repair the Apple-vz SVE trap (arm64.nosve grub drop-in) on
        # VMs provisioned before the create-time bootstrap mask existed. Runs
        # early, before the crypto-dependent apt/source steps, so a broken VM at
        # least gets the fix installed this pass; it needs a restart plus one
        # more reinit to converge. A silent no-op on every non-Apple host and on
        # already-masked VMs.
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
        # Init runs against a VM that exists in the DB (bootstrap_vm fetches the
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
        _configure_apt_sources(
            ts_target,
            vm_template,
            apt_packages,
            apt_sources,
            logger,
            debian_release=debian_release,
            resolved_sources=resolved_apt_sources,
        )

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

            ensure_agent_socket_root(
                ts_target,
                admin_username,
                warn_if_missing=operation is not VMInitializationOperation.VM_CREATE,
            )
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

        # Non-fatal: tighten the admin's home to 0750 (mirrors the agent-home
        # hardening in agents/initializer.py). Runs on both initial provision
        # and reinit (both reach _phase_b_setup), so a pre-existing
        # world-readable admin home is repaired on the next reinit.
        _harden_admin_home(ts_target, home=home, admin_username=admin_username, logger=logger)

        # Non-fatal: the shared workspaces parent directory and its canonical
        # ACL (recursive over all workspaces). The ACL apply and the
        # parent-traversal re-grant are order-dependent, so they live in one
        # helper that documents and enforces the order (see #254).
        _setup_workspaces_directory(ts_target, config, logger)

        # Non-fatal: mise config (written before dotfiles so dotfiles can override)
        mise_path: list[str] = _mise_shims_path(home)
        if admin.mise_packages:
            _write_mise_config(ts_target, admin.mise_packages, admin.mise_install_before, home, logger)

        # Non-fatal: git safe.directory wildcard (disables ownership checks for the
        # multi-user workspace model where agents access repos owned by admin)
        if admin.git_force_safe_directory:
            try:
                ensure_safe_directory_wildcard(ts_target)
                output.info("Git safe.directory wildcard configured")
            except SSHError as e:
                msg = f"git safe.directory setup failed: {e}"
                logger.warning(msg)
                output.warn(msg)

        # Non-fatal: git credentials (before dotfiles and mise lockfile for private repos)
        configure_user_git_credentials(
            ts_target,
            credential_requests,
            config,
            logger,
            target_role="admin",
        )

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

        # Final remote mutation: once this succeeds, no later Phase B work can
        # make the identity proof stale before the local stability check and
        # transactional terminal checkpoint.
        return _reconcile_authorized_keys(ts_target, config, home, logger)


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
