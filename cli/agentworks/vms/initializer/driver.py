"""The two-phase init driver: Phase A (first contact and the VM contract) and
Phase B (setup, non-fatal-on-failure), both over ``agentworks.execution``.

Phase A is provisioning: after the platform completes create-time bootstrap,
``bootstrap_vm`` runs ``phase_a.bootstrap`` (record the address, enroll the
host key, check the contract) and syncs SSH config, then returns the admin's
``VMAccess`` and home for Phase B. Every Phase B request runs on that one
access, each bounded by ``steps.STEP_DEADLINE`` unless it has a bound of its
own. Until the Admin Initialization steps move, those steps and the final
``authorized_keys`` write still take the legacy target the callers build
beside the access. Phase B is initialization:
``run_initialization`` runs it, both after ``bootstrap_vm`` on ``vm create``
and standalone on ``vm reinit``.

``create_vm`` (in ``vms.manager.lifecycle``) drives ``bootstrap_vm`` inside
its ``Provisioning`` output section and ``run_initialization`` after it, so
the phase boundary matches the section boundary; it also owns the keepalive
hold spanning both phases and the create-vs-init error mapping. This driver
owns only the per-phase step sequences and status/event bookkeeping.
"""

from __future__ import annotations

import shlex
from enum import StrEnum
from typing import TYPE_CHECKING

import agentworks.execution as execution
from agentworks import output
from agentworks.db import InitStatus
from agentworks.env import ResourceContext, vm_stable_identity_env
from agentworks.execution import trust
from agentworks.execution.access import Admin
from agentworks.execution.requests import Command
from agentworks.git_config import ensure_safe_directory_wildcard
from agentworks.git_credentials import configure_user_git_credentials
from agentworks.ssh import SSHError, SSHLogger
from agentworks.transports import SSHTransport, Transport

from . import phase_a
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
    AuthorizedKeysUnproven,
    _apply_sve_mask,
    _preserve_ssh_host_keys,
    _reconcile_authorized_keys,
)
from .steps import STEP_DEADLINE, STEP_FAILURES, warn_step
from .workspaces_dir import _setup_workspaces_directory

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, DesiredOverlayRecord
    from agentworks.db.instance_state import VersionedPayload
    from agentworks.debian import DebianRelease
    from agentworks.execution.access import VMAccess
    from agentworks.execution.files import Files
    from agentworks.git_credentials import CredentialRequest
    from agentworks.harness_setup.inputs import SetupInputs
    from agentworks.harness_setup.locking import NativeMutationGuard
    from agentworks.harness_setup.outcomes import SetupFailure
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
    platform: VMPlatform,
    ctx: RunContext,
    logger: SSHLogger,
    *,
    admin_username: str,
    tailnet_address: str,
    expected_release: DebianRelease,
) -> tuple[VMAccess, Transport, str, DebianRelease]:
    """Run Phase A (``phase_a.bootstrap``), then hand Phase B the admin's access.

    Phase A records the platform's address, makes the first contact in Enroll
    mode and runs the VM contract, release first, all over
    ``agentworks.execution``; see :mod:`.phase_a` for its fail-closed span.
    The non-fatal SSH-config sync follows.

    Returns ``(access, legacy_target, home, release)``: the admin's access,
    built from the row Phase A recorded the address on, the legacy target the
    Admin Initialization steps still take beside it, the home for
    ``run_initialization``, and the release Phase A attested. The caller owns
    the availability hold spanning both phases, the ``Provisioning`` output
    section and ``logger``'s lifetime.
    """
    address, release = phase_a.bootstrap(
        db,
        config,
        vm_name,
        platform,
        ctx,
        logger,
        admin_username=admin_username,
        tailnet_address=tailnet_address,
        expected_release=expected_release,
    )

    # The operator's ``ssh awvm--<name>`` aliases go in before Phase B's many
    # connections, and a failed write continues into Phase B.
    from agentworks.ssh_config import sync_ssh_config_or_warn

    sync_ssh_config_or_warn(config, db)

    vm = db.get_vm(vm_name)
    assert vm is not None, f"VM '{vm_name}' missing from DB after Phase A"
    access = execution.vm_access(vm, config, Admin(), log=logger.record)
    legacy_target = SSHTransport(
        host=address,
        trust=trust.TrustBinding(trust.TrustMode.VM, vm_name),
        user=admin_username,
        identity_file=config.operator.ssh_private_key,
        default_timeout=STEP_DEADLINE,
        logger=logger,
    )
    return access, legacy_target, f"/home/{admin_username}", release


def run_initialization(
    db: Database,
    config: Config,
    registry: Registry,
    vm_template: ResolvedVMTemplate,
    admin: AdminConfig,
    vm_name: str,
    access: VMAccess,
    credential_requests: tuple[CredentialRequest, ...],
    home: str,
    admin_username: str,
    logger: SSHLogger,
    *,
    legacy_target: Transport,
    debian_release: DebianRelease,
    operation: VMInitializationOperation,
    setup_failures: list[SetupFailure],
    setup_inputs: tuple[SetupInputs, ...] = (),
    setup_guard: NativeMutationGuard | None = None,
    setup_values: Mapping[str, str] | None = None,
    legacy_conversion: tuple[DesiredOverlayRecord, VersionedPayload] | None = None,
) -> None:
    """Run Phase B (initialization) with status tracking and event logging.

    This is called both from ``create_vm`` (after ``bootstrap_vm``) and
    from ``reinit_vm`` for repeatable re-initialization. The closed
    ``operation`` value also drives create-only setup behavior.
    Each credential request carries the provider's scoped context assembler.
    Native setup failures join the caller's collector and leave initialization
    partial after terminal key reconciliation and checkpointing. The caller
    reports them after its remaining command bookkeeping.

    ``access`` is the admin's, and every step's requests run on it, bounded
    by ``STEP_DEADLINE`` through one ``Files``. ``legacy_target`` is the
    admin's legacy transport, which the Admin Initialization steps and the
    final ``authorized_keys`` write take until they move onto ``access``.
    """
    from agentworks.harness_setup.locking import native_mutation_guard

    with native_mutation_guard(db.path, vm_name, held=setup_guard) as guard:
        from agentworks.harness_setup.lifecycle import require_prepared_setup

        require_prepared_setup(db, "vm", vm_name, ("vm", "admin"), setup_inputs)
        db.insert_vm_event(vm_name, "init_started")

        files = access.files(deadline=STEP_DEADLINE)
        try:
            _phase_b_setup(
                db,
                config,
                registry,
                vm_template,
                admin,
                vm_name,
                files,
                credential_requests,
                home,
                admin_username,
                logger,
                legacy_target=legacy_target,
                debian_release=debian_release,
                operation=operation,
            )
            if setup_inputs:
                from agentworks.harness_setup.lifecycle import apply_vm_setup

                vm = db.get_vm(vm_name)
                assert vm is not None
                apply_vm_setup(
                    db,
                    files,
                    registry,
                    inputs=setup_inputs,
                    vm=vm,
                    values=setup_values or {},
                    logger=logger,
                    held=guard,
                    operation=operation.value,
                    failures=setup_failures,
                )
            # Terminal guest mutation follows every integration's setup.
            authorized_keys = _reconcile_authorized_keys(legacy_target, config, home, logger)
        except Exception as e:
            with db.transaction():
                db.update_vm_init_status(vm_name, InitStatus.FAILED)
                db.insert_vm_event(vm_name, "init_failed", logger.sanitize(str(e)))
            raise

        if isinstance(authorized_keys, AuthorizedKeysUnproven):
            output.warn(
                f"SSH identity evidence for VM '{vm_name}' is unknown, so ordinary SSH commands "
                "will refuse to connect. If the configured key still works, retry with "
                f"'agw vm reinit {vm_name}'. Otherwise, run 'agw vm recover {vm_name}', which "
                "restores the configured key through the platform's own channel, then reinitialize. "
                "If recovery cannot reach the VM, recreate it."
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

        partial = bool(setup_failures) or logger.has_warnings
        final_status = InitStatus.PARTIAL if partial else InitStatus.COMPLETE
        final_event = "init_partial" if partial else "init_complete"
        details = []
        if setup_failures:
            details.append(f"{len(setup_failures)} setup failure(s)")
        if logger.has_warnings:
            details.append(f"{len(logger.warnings)} warning(s)")
        final_detail = "; ".join(details) or None

        # Do not include this checkpoint in the Phase B exception arm above: a
        # local database failure must roll back the complete terminal result and
        # propagate, leaving the earlier in-progress evidence intact.
        with db.transaction():
            if legacy_conversion is not None and final_status is InitStatus.COMPLETE:
                from agentworks.legacy_claude import checkpoint_conversion

                checkpoint_conversion(db, *legacy_conversion)
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


def _phase_b_setup(
    db: Database,
    config: Config,
    registry: Registry,
    vm_template: ResolvedVMTemplate,
    admin: AdminConfig,
    vm_name: str,
    files: Files,
    credential_requests: tuple[CredentialRequest, ...],
    home: str,
    admin_username: str,
    logger: SSHLogger,
    *,
    legacy_target: Transport,
    debian_release: DebianRelease,
    operation: VMInitializationOperation,
) -> None:
    """Phase B: setup on the admin's ``files.access``. Non-fatal steps warn and continue."""
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
        _preserve_ssh_host_keys(files, logger)

        # Non-fatal: repair the Apple-vz SVE trap (arm64.nosve grub drop-in) on
        # VMs provisioned before the create-time bootstrap mask existed. Runs
        # early, before the crypto-dependent apt/source steps, so a broken VM at
        # least gets the fix installed this pass; it needs a restart plus one
        # more reinit to converge. A silent no-op on every non-Apple host and on
        # already-masked VMs.
        _apply_sve_mask(files, logger)

        # Non-fatal: VM hardening (sysctl baseline + /proc hidepid>=1).
        # Runs before the rest of init so subsequent steps execute under the
        # hardened baseline. Depends only on coreutils + procps (always
        # present); nothing here needs apt-installed packages. Idempotent on
        # reinit.
        from agentworks.vms.hardening import apply_vm_hardening

        apply_vm_hardening(files, logger)

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

        check_vm_dns(files, logger)

        # Non-fatal: tailscaled cold-boot DNS race fix (GitHub issue #117).
        # Drops in a systemd override that orders tailscaled after the DNS
        # layer is up so its DNS-manager probe finds a resolver instead of
        # falling back to direct mode. Applied early in Phase B so existing
        # VMs pick up the fix on the first reinit. Does not restart
        # tailscaled (would disconnect us); takes effect on next cold boot.
        apply_tailscaled_dns_fix(files, logger)

        # Non-fatal: VM-wide SetEnv plumbing.
        # Runs before apt install so subsequent SSH commands within init can
        # rely on the SetEnv path. These targets don't touch zsh-shipped files,
        # so dpkg conffile handling doesn't apply.
        _write_sshd_accept_env(files, logger)
        _write_sudoers_env_keep(files, logger)
        # Pairs with the --preserve-env in _split_shell_pane's agent-pane branch.
        _write_sudoers_console_setenv(files, logger, admin_username)
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

        # Required system packages must succeed before dependent setup runs.
        _install_system_packages(files, logger)

        # Non-fatal: apt sources required by selected apt_packages
        _configure_apt_sources(
            files,
            vm_template,
            apt_packages,
            apt_sources,
            logger,
            debian_release=debian_release,
            resolved_sources=resolved_apt_sources,
        )

        # Non-fatal: apt packages (direct list + apt-package entries)
        _install_apt_packages(files, vm_template, apt_packages, logger)

        # Identity profile fragments. Runs AFTER apt install because apt uses
        # `--force-confnew`, which would replace the agentworks block in
        # `/etc/zsh/zprofile` with zsh-common's package default if zsh got
        # installed after we wrote our fragment. Post-install, we append cleanly
        # on top of whatever the package shipped. The mirror is idempotent on
        # reinit (strip-and-rewrite via begin/end markers).
        _write_agentworks_identity_profile(
            files,
            vm_stable_identity_env(identity_ctx),
            logger,
        )

        # /etc/skel seeds. MUST run AFTER apt for the same reason as the
        # identity profile above: `/etc/skel/.bashrc` is a Debian conffile
        # shipped by the `bash` package. Running before apt's
        # `--force-confnew` would let a bash upgrade silently replace the
        # seed with Debian's stock skel (saving ours as .dpkg-old). Future
        # `useradd -m` would then inherit Debian's skel instead.
        _write_skel_seeds(files, logger)

        # Non-fatal: snap packages
        if vm_template.snap:
            logger.step("Snap packages")
            output.info(f"Installing {output.count(len(vm_template.snap), 'snap package')}...")
            for pkg in vm_template.snap:
                try:
                    files.access.run(Command(("snap", "install", pkg), elevated=True, deadline=120), check=True)
                except STEP_FAILURES as error:
                    warn_step(logger, f"snap install '{pkg}' failed", error)

        # admin_shell is a pure config read, hoisted above the system install
        # commands below (which run in it) so they stay in the VM section; the
        # login-shell usermod is a separate admin step further down.
        admin_shell = admin.shell

        # Non-fatal: system install commands (VM-level, system-wide). Kept in
        # the VM section: they run via ``{admin_shell} -lc`` explicitly, so
        # they do not depend on the login-shell usermod, and they install
        # system-wide tools rather than touching the admin's rc.
        system_path = _run_install_commands(
            files,
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
                files,
                admin_username,
                warn_if_missing=operation is not VMInitializationOperation.VM_CREATE,
            )
            for agent in db.list_agents(vm_name=vm_name):
                ensure_agent_socket_dir(files, agent.linux_user)
                removed = cleanup_stale_sockets(files, agent.linux_user)
                if removed:
                    output.detail(f"Cleaned up {output.count(removed, 'stale socket')} for {agent.linux_user}")
        except STEP_FAILURES as error:
            warn_step(logger, "agent tmux socket setup failed", error)

    with output.section("Admin Initialization"):
        # Non-fatal: set default shell (before the USER install commands so
        # those installers write to the correct rc file). The zsh
        # ``zsh-newuser-install`` first-run wizard is pre-empted by the skel seed.
        logger.step("Shell configuration")
        output.info(f"Setting shell to {admin_shell}...")
        try:
            legacy_target.run(
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
        _harden_admin_home(legacy_target, home=home, admin_username=admin_username, logger=logger)

        # Non-fatal: the shared workspaces parent directory and its canonical
        # ACL (recursive over all workspaces). The ACL apply and the
        # parent-traversal re-grant are order-dependent, so they live in one
        # helper that documents and enforces the order (see #254).
        _setup_workspaces_directory(files, config, logger)

        # Non-fatal: mise config (written before dotfiles so dotfiles can override)
        mise_path: list[str] = _mise_shims_path(home)
        if admin.mise_packages:
            _write_mise_config(legacy_target, admin.mise_packages, admin.mise_install_before, home, logger)

        # Non-fatal: git safe.directory wildcard (disables ownership checks for the
        # multi-user workspace model where agents access repos owned by admin)
        if admin.git_force_safe_directory:
            try:
                ensure_safe_directory_wildcard(legacy_target)
                output.info("Git safe.directory wildcard configured")
            except SSHError as e:
                msg = f"git safe.directory setup failed: {e}"
                logger.warning(msg)
                output.warn(msg)

        # Non-fatal: git credentials (before dotfiles and mise lockfile for private repos)
        configure_user_git_credentials(
            legacy_target,
            files.access,
            credential_requests,
            config,
            logger,
            target_role="admin",
        )

        # Non-fatal: dotfiles (can override mise config, can provide lockfile).
        # Sync and install are reported separately because a failed sync skips
        # the install entirely. Saying so is the difference between a warning an
        # operator can act on and a later failure that looks unrelated to it.
        if admin.dotfiles_source:
            logger.step("Dotfiles")
            dest = admin.dotfiles_destination.replace("~", home)
            synced = False
            try:
                from agentworks.sources import fetch_dir, parse_source_ref

                ref = parse_source_ref(admin.dotfiles_source)
                output.info(f"Syncing dotfiles from {admin.dotfiles_source}...")
                fetch_dir(ref, legacy_target, dest, logger=logger)
                synced = True
            except Exception as e:
                msg = (
                    f"dotfiles sync failed, so '{admin.dotfiles_install_cmd}' did not run and {dest} "
                    f"still holds its previous contents: {e}"
                )
                logger.warning(msg)
                output.warn(msg)
            if synced:
                try:
                    output.info(f"Running dotfiles install: {admin.dotfiles_install_cmd}")
                    legacy_target.run(
                        f"cd {dest} && {admin.dotfiles_install_cmd}",
                        timeout=120,
                    )
                except Exception as e:
                    msg = f"dotfiles install '{admin.dotfiles_install_cmd}' failed in {dest}: {e}"
                    logger.warning(msg)
                    output.warn(msg)

        # Non-fatal: mise lockfile (after git creds and dotfiles; overrides dotfiles lockfile)
        if admin.mise_lockfile:
            _fetch_mise_lockfile(legacy_target, admin.mise_lockfile, home, logger)

        # Non-fatal: mise install (after config + dotfiles + lockfile are all settled)
        prune = admin.mise_prune_on_reinit
        if admin.mise_packages or admin.mise_lockfile:
            _run_mise_install(
                legacy_target,
                admin_shell,
                home,
                admin.mise_allow_unlocked,
                logger,
                prune=prune,
            )
        else:
            try:
                check = legacy_target.run(f"test -f {home}/.config/mise/config.toml", check=False)
                if check.ok:
                    _run_mise_install(
                        legacy_target,
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
            files,
            admin.user_install_commands,
            user_install_commands,
            admin_shell,
            home,
            logger,
            label="User install command",
        )

        # Non-fatal: shell profile (PATH exports sourced at login)
        all_paths = system_path + mise_path + user_path
        _write_agentworks_profile(legacy_target, all_paths, logger)

        # Non-fatal: shell rc (interactive shell hooks like mise activate)
        rc_snippets = [MISE_ACTIVATE_LINES] if admin.mise_activate else ["# mise activation disabled"]
        _write_agentworks_rc(legacy_target, rc_snippets, logger)

        # Defensive final step: re-ensure source lines in case any earlier
        # step (dotfiles install in particular) overwrote a shell rc file
        # in place. Idempotent grep-or-append.
        _ensure_agentworks_files_sourced(
            legacy_target,
            home=home,
            shell=admin_shell,
            logger=logger,
        )
