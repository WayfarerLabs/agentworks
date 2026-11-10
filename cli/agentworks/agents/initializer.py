"""On-VM agent provisioning: the SSH bodies that create, configure,
and remove an agent's Linux user.

The agent counterpart of ``agentworks.vms.initializer``. Command orchestration
(validation, gating, DB rows, rollback choreography) lives in
``agentworks.agents.manager`` and ``agents/realize.py``; this module owns the
remote mutations they drive over SSH: the user bootstrap and
self-configure sequence, its install-command / profile / rc / mise
sub-steps, and the user removal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.execution as execution
from agentworks import output
from agentworks.execution.access import Admin, Agent
from agentworks.execution.requests import Command, Script, Shell
from agentworks.git_config import ensure_safe_directory_wildcard
from agentworks.transports import transport
from agentworks.vms.initializer.mise import MISE_ACTIVATE_LINES
from agentworks.vms.initializer.shell_env import (
    _ensure_agentworks_files_sourced,
    _write_agentworks_profile,
    _write_agentworks_rc,
)
from agentworks.vms.initializer.steps import STEP_FAILURES, describe_failure

if TYPE_CHECKING:
    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.execution.access import VMAccess
    from agentworks.execution.files import Files
    from agentworks.execution.requests import AccountShell, RunRequest
    from agentworks.git_credentials import CredentialRequest
    from agentworks.resources import Registry
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport
    from agentworks.vms.initializer.steps import InitLog


def create_new_agent_user(
    vm: VMRow,
    config: Config,
    linux_user: str,
    *,
    shell: AccountShell,
    logger: SSHLogger,
) -> None:
    """Establish fresh ownership before the caller may arm user rollback.

    Refuse an unowned account or home, including dangling symlinks. A competing
    useradd fails exclusively; the caller must not delete anything on that failure.
    Existing DB-owned agents instead use create_agent_on_vm's convergence path.
    """
    import shlex

    from agentworks.errors import StateError

    admin = execution.vm_access(vm, config, Admin(), log=logger.record)
    home = f"/home/{linux_user}"
    account = Command(("getent", "passwd", linux_user), elevated=True)
    home_entry = Script(f"test -e {shlex.quote(home)} || test -L {shlex.quote(home)}", Shell.SH, elevated=True)
    if _status(admin, account) == 0 or _status(admin, home_entry) == 0:
        raise StateError(
            f"cannot create agent: unowned user or home already exists for '{linux_user}'",
            entity_kind="agent",
            entity_name=linux_user,
            hint="Choose another agent name or remove the unowned native account and home first.",
        )
    admin.run(Command(("useradd", "-m", "-U", "-s", f"/bin/{shell}", linux_user), elevated=True), check=True)


def _status(access: VMAccess, request: RunRequest) -> int:
    """Run ``request`` and return the guest's exit status; an outcome that is not a completion raises."""
    return access.run(request).require_completed(access.vm_name)


def create_agent_on_vm(
    vm: VMRow,
    config: Config,
    registry: Registry,
    agent_tmpl: ResolvedAgentTemplate,
    linux_user: str,
    *,
    agent_name: str,
    credential_requests: tuple[CredentialRequest, ...],
    logger: SSHLogger,
    admin_git_force_safe_directory: bool,
) -> None:
    """Create an agent Linux user on a VM and configure their environment.

    Workspace group membership is NOT set here; it is managed by the
    grant system. This function only creates the user and configures
    their tools.

    The work splits cleanly into two phases by who is running each step:

    1. **Bootstrap (admin)**: ``useradd`` / ``usermod`` → tmux socket
       infrastructure under ``/var/lib/`` → install ``authorized_keys``
       via stage-and-install. The last step is what makes direct agent
       SSH possible from this point onward. This is the only admin work
       in agent create / reinit.
    2. **Self-configure (agent)**: every subsequent step runs over the
       agent's own SSH session against ``agent_target``. Covers rc /
       profile, git config + credentials, dotfiles, install commands,
       mise. The agent owns its home, so no sudo or
       cross-uid file writes are needed in this phase.

    Keeping these two phases disjoint by transport (admin_target vs.
    agent_target) minimizes the code surface that runs as root on the
    agent's behalf and matches the rule that operations whose target
    user is the agent open SSH directly as the agent's Linux user.
    """
    # DRIFT GUARD (Phase 7, recipe use-gate): this function is itself never
    # gated; it runs ``_run_agent_install_commands`` (a declarable consumer), so
    # the recipe gate (``ensure_recipe_enabled``) must sit at each COMMAND ENTRY
    # that reaches it (agent create/reinit, session create --new-agent). If you
    # add a NEW caller of ``create_agent_on_vm``, add its command-entry gate and
    # update tests/agents/test_recipe_gate_drift.py's enumerated caller set.
    from agentworks.sessions.tmux import (
        cleanup_stale_sockets,
        ensure_agent_socket_dir,
        ensure_agent_socket_root,
    )
    from agentworks.transports import transport_for_user
    from agentworks.vms.initializer import _reconcile_authorized_keys

    admin = execution.vm_access(vm, config, Admin(), log=logger.record)
    admin_files = admin.files()

    home = f"/home/{linux_user}"

    agent_cfg = agent_tmpl
    agent_shell = agent_cfg.shell

    # -- Phase 1: bootstrap (admin) ---------------------------------------

    # Create-or-converge the agent's Linux user, reporting the TRUE
    # outcome rather than announcing a create every time. This body is
    # shared by ``agent create`` (user usually absent) and ``agent
    # reinit`` (user usually present), so the message is driven by the
    # DETECTED state, not by which command called us: the case that must
    # not be hidden is a truly-gone agent user being recreated on reinit,
    # which the old unconditional "Creating user..." line silently masked
    # as a converging no-op (issue #252). A single ``getent passwd`` probe
    # yields both existence (exit status) and the current login shell
    # (field 7), the one attribute the ``usermod`` convergence below
    # touches, so the three reported outcomes (created / already-exists /
    # shell-corrected) each reflect what actually happened on the VM.
    shell_path = f"/bin/{agent_shell}" if "/" not in agent_shell else agent_shell
    # ``-U`` (``--user-group``) forces a per-user private primary group
    # regardless of the base image's ``USERGROUPS_ENAB`` setting. This is
    # load-bearing for the 0750 home below: 0750 is only agent-private if
    # the group half grants access to nobody but this user. On an image
    # where new users land in a shared primary group (e.g. ``users``,
    # GID 100), a bare ``useradd`` plus 0750 would GRANT every other agent
    # group read+execute on this home, an inversion of the goal. We do
    # not assume a clean Debian image (see capabilities/vm_platform/README).
    passwd_entry = admin.run(Command(("getent", "passwd", linux_user), elevated=True))
    if passwd_entry.require_completed(vm.name) != 0:
        admin.run(Command(("useradd", "-m", "-U", "-s", shell_path, linux_user), elevated=True), check=True)
        output.info(f"Created agent user '{linux_user}' on VM '{vm.name}'")
    else:
        # ``getent passwd`` line is colon-delimited with the login shell
        # last; the leading fields (name, gecos, home) never contain a
        # colon, so the final field is the shell. Take the first line in
        # case NSS ever returns more than one entry for the name.
        current_shell = passwd_entry.stdout.decode("utf-8", "replace").strip().splitlines()[0].rsplit(":", 1)[-1]
        if current_shell != shell_path:
            admin.run(Command(("usermod", "-s", shell_path, linux_user), elevated=True), check=True)
            output.info(f"Fixed agent user '{linux_user}' on VM '{vm.name}': shell {current_shell} -> {shell_path}")
        else:
            output.info(f"Agent user '{linux_user}' already exists on VM '{vm.name}'")

    # Tighten the agent's home to 0750. ``useradd -m`` honors the system
    # umask (022), leaving home world-readable (0755), which lets any
    # other agent user on the VM read this agent's scratch artifacts,
    # logs, shell history, cloned repos, and tool caches. Per-agent Linux
    # users exist to keep agent credentials and state separate, so a
    # world-readable $HOME undercuts the isolation. With the private
    # primary group enforced above, 0750 is effectively owner-only.
    # Idempotent: a no-op when already 0750, so it also fixes pre-existing
    # agents on reinit. Kept in phase 1 on the admin's access: this runs
    # before authorized_keys is installed, so the agent's own access does
    # not work yet.
    admin.run(Command(("chmod", "0750", home), elevated=True), check=True)

    # Post-condition guard: verify the primary group really is private.
    # ``-U`` enforces this on the create path, but reinit, pre-existing
    # agents created before ``-U`` was added, and odd images can still
    # leave a shared primary group, which would make the 0750 home
    # group-readable by whoever shares that group. Warn (do not fail) so
    # the drift is surfaced with a fix hint rather than silently defeating
    # isolation: a hard fail here could block a legitimately custom setup
    # or a partial-state reinit, and ``-U`` already covers fresh creates.
    primary_group = admin.run(Command(("id", "-gn", linux_user), elevated=True))
    group_name = primary_group.stdout.decode("utf-8", "replace").strip()
    if primary_group.require_completed(vm.name) == 0 and group_name != linux_user:
        output.warn(
            f"agent '{linux_user}' primary group is "
            f"'{group_name}', not a private per-user group; "
            f"its home ({home}) cannot be made private and may be readable by "
            f"other members of that group. Fix on the VM with "
            f"'sudo groupadd {linux_user} && sudo usermod -g {linux_user} {linux_user}' "
            f"and reinit, or check the image's group model."
        )

    # Tmux socket infrastructure for the agent (root-owned ``/run/agentworks/``
    # parent; only the admin's access can create it).
    # ensure_agent_socket_root first so this works on VMs that haven't
    # been reinited since the socket feature was added. The per-agent
    # dir won't exist for a brand-new agent, so we suppress the
    # "missing" warning; misconfiguration of an existing dir still warns.
    ensure_agent_socket_root(admin_files, vm.admin_username)
    ensure_agent_socket_dir(admin_files, linux_user, warn_if_missing=False)
    removed = cleanup_stale_sockets(admin_files, linux_user)
    if removed:
        output.detail(f"Cleaned up {output.count(removed, 'stale socket')}")

    # Reconcile authorized_keys as root, with the agent as owner. The only
    # admin work that lands content INTO the agent's home; everything below
    # is the agent writing to its own home over its own access.
    _reconcile_authorized_keys(admin_files, config, home=home, log=logger, owner=linux_user)

    # -- Phase 2: self-configure (agent) ----------------------------------

    agent_target = transport_for_user(vm, config, user=linux_user, logger=logger)
    agent_access = execution.vm_access(vm, config, Agent(agent_name, linux_user), log=logger.record)
    agent_files = agent_access.files()

    # Provisioning is hermetic: no operator env from [agent_templates.*.env]
    # or [vm_templates.*.env] is injected into the agent's install runners.
    # Static identity (AGENTWORKS_VM via /etc/profile.d/, AGENTWORKS_AGENT
    # via the per-user ~/.agentworks-profile.sh we write BELOW before the
    # install commands run) reaches the runners through login-shell
    # sourcing. Integration setup receives operator env through its own runner.

    # Write the agent's per-user profile fragment EARLY -- before any
    # install commands run -- so that AGENTWORKS_AGENT is visible to
    # those commands via the login-shell sourcing chain. The fragment
    # gets rewritten at the end of _run_agent_install_commands with
    # accumulated PATH entries from user install commands.
    from agentworks.env import ResourceContext, per_user_identity_env
    from agentworks.vms.sites import site_platform_name

    agent_identity_ctx = ResourceContext(
        vm_name=vm.name,
        platform=site_platform_name(vm.site, registry),
        site=vm.site,
        user=linux_user,
        agent_name=agent_name,
    )
    agent_identity = per_user_identity_env(agent_identity_ctx)
    _write_agentworks_profile(agent_files, [], logger, home=home, shell=agent_shell, identity_env=agent_identity)

    # Always write ~/.agentworks-rc.sh -- even when there are no shell
    # hooks to install. The defensive ``_ensure_agentworks_files_sourced``
    # step at the end of setup adds a ``. ~/.agentworks-rc.sh`` line to
    # the agent's .bashrc/.zshrc unconditionally; if the file doesn't
    # exist, every interactive login hits "No such file or directory".
    snippet = MISE_ACTIVATE_LINES if agent_cfg.mise_activate else "# mise activation disabled"
    _write_agentworks_rc(agent_files, [snippet], logger, home=home, shell=agent_shell)

    # No PS1 setup: operators who want an agent indicator can read
    # $AGENTWORKS_AGENT (exported by the per-user profile fragment we
    # just wrote) from their own prompt. A hardcoded PS1 lost against
    # starship / powerlevel10k anyway, and clobbered symlinked dotfiles
    # via scp.

    # Git safe.directory wildcard (agents access repos owned by admin).
    # The caller passes the VM's effective admin declaration, including
    # its stored final layer, so this path cannot silently fall back to a
    # plain selected template.
    if admin_git_force_safe_directory:
        try:
            ensure_safe_directory_wildcard(agent_files)
            output.info("Git safe.directory configured for agent")
        except STEP_FAILURES as error:
            output.warn(f"agent git safe.directory setup failed: {describe_failure(error, logger.sanitize)}")

    # Credential reconciliation is unconditional. An empty declared list
    # removes any Agentworks-owned state left by an older template.
    from agentworks.git_credentials import configure_user_git_credentials

    configure_user_git_credentials(
        agent_access,
        credential_requests,
        config,
        logger,
        target_role="agent",
    )

    # User install commands + login-shell PATH profile fragment.
    _run_agent_install_commands(
        agent_files=agent_files,
        logger=logger,
        registry=registry,
        agent_tmpl=agent_tmpl,
        home=home,
        identity_env=agent_identity,
    )

    # Dotfiles.
    if agent_cfg.dotfiles_source:
        output.info(f"Syncing agent dotfiles from {agent_cfg.dotfiles_source}...")
        try:
            import shlex as _shlex

            from agentworks.sources import SourceRefError, expand_destination, fetch_dir, parse_source_ref

            ref = parse_source_ref(agent_cfg.dotfiles_source)
            dest = expand_destination(agent_cfg.dotfiles_destination, home)

            if ref.kind == "git":
                # If already cloned from the same repo, pull instead of clone.
                is_git = agent_target.run(
                    f"test -d {_shlex.quote(dest)}/.git",
                    check=False,
                )
                if is_git.ok:
                    remote = agent_target.run(
                        f"git -C {_shlex.quote(dest)} remote get-url origin",
                        check=False,
                    )
                    if remote.ok and remote.stdout.strip() == ref.path:
                        output.detail("Dotfiles already cloned, pulling latest...")
                        if ref.ref:
                            agent_target.run(
                                f"git -C {_shlex.quote(dest)} fetch",
                                check=False,
                                timeout=120,
                            )
                            checkout = agent_target.run(
                                f"git -C {_shlex.quote(dest)} checkout {_shlex.quote(ref.ref)}",
                                check=False,
                            )
                            if not checkout.ok:
                                output.warn(f"dotfiles checkout of '{ref.ref}' failed, skipping")
                        else:
                            pull = agent_target.run(
                                f"git -C {_shlex.quote(dest)} pull",
                                check=False,
                                timeout=120,
                            )
                            if not pull.ok:
                                output.warn("dotfiles pull failed (local changes?), skipping")
                    else:
                        raise SourceRefError(f"dotfiles destination {dest} exists but is a different repo")
                else:
                    clone_cmd = f"git clone {_shlex.quote(ref.path)} {_shlex.quote(dest)}"
                    if ref.ref:
                        clone_cmd = (
                            f"git clone --branch {_shlex.quote(ref.ref)} {_shlex.quote(ref.path)} {_shlex.quote(dest)}"
                        )
                    agent_target.run(clone_cmd, timeout=120)
            else:
                # Local source: fetch directly into the agent's home over
                # agent SSH. The agent owns dest, so no sudo / chown
                # dance. fetch_dir handles existing-dest overwrite.
                fetch_dir(ref, agent_files, dest)

            output.info(f"Running agent dotfiles install: {agent_cfg.dotfiles_install_cmd}")
            # Wrap in a login shell so the dotfiles install command sees
            # static identity (AGENTWORKS_AGENT via the per-user profile
            # fragment written earlier this phase) and any PATH the agent
            # already has. Provisioning is hermetic: no operator env
            # injected (would only reach runtime shells anyway).
            inner = f"cd {_shlex.quote(dest)} && {agent_cfg.dotfiles_install_cmd}"
            agent_target.run(
                f"{agent_shell} -lc {_shlex.quote(inner)}",
                timeout=120,
            )
        except (SourceRefError, Exception) as e:
            output.warn(f"agent dotfiles failed: {e}")

    # Mise.
    _run_agent_mise_setup(agent_target=agent_target, agent_files=agent_files, agent_tmpl=agent_tmpl, home=home)

    # Defensive final step: re-ensure source lines in case dotfiles
    # install (or any other later step) overwrote a shell rc file in
    # place. A file that already names a fragment is left alone.
    _ensure_agentworks_files_sourced(agent_files, logger, home=home, shell=agent_shell)


def delete_agent_on_vm(
    vm: VMRow,
    config: Config,
    linux_user: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Remove an agent Linux user from a VM, along with related on-VM
    state: their processes are killed, the user and home directory are
    removed, and the per-agent tmux socket directory is cleaned up.
    """
    import shlex

    from agentworks.sessions.tmux import agent_socket_dir
    from agentworks.ssh import SSHError

    target = transport(vm, config, logger=logger)

    try:
        # Kill any running processes for the user. This also frees any live
        # tmux sockets under the per-agent socket dir removed next.
        target.run(f"pkill -u {linux_user}", sudo=True, check=False)
        # Remove the per-agent tmux socket directory (created by
        # ``ensure_agent_socket_dir`` at create time). It lives on tmpfs
        # under /run, is owned by the agent uid, and would otherwise linger
        # until reboot. Done BEFORE userdel and best-effort (check=False) so
        # that a userdel failure (e.g. exit 12, home or mail-spool cleanup)
        # cannot skip it and re-orphan the dir under the freed uid. The dir
        # is separate from the home directory userdel removes, and the
        # sockets are already dead from the pkill above, so this ordering is
        # safe.
        target.run(f"rm -rf {shlex.quote(agent_socket_dir(linux_user))}", sudo=True, check=False)
        # Remove the user and their home directory
        target.run(f"userdel -r {linux_user}", sudo=True)
    except SSHError as e:
        output.warn(f"remote cleanup for '{linux_user}' failed: {e}")


def _run_agent_install_commands(
    *,
    agent_files: Files,
    logger: InitLog,
    registry: Registry,
    agent_tmpl: ResolvedAgentTemplate,
    home: str,
    identity_env: dict[str, str],
) -> None:
    """Run user install commands as the agent and rewrite its profile fragment with the accumulated PATH.

    The commands run through Phase B's runner (``_run_install_commands``) on
    the agent's own access: a command whose every declared completion test
    passes is skipped, and a failure warns but does not abort. The profile
    fragment is rewritten unconditionally (even when there are no install
    commands and no PATH additions) so that reinit can clear previously set
    paths, with mise's shims last when mise is configured, so they take
    precedence.

    Install commands run without any injected environment: provisioning is
    hermetic. Static identity (``AGENTWORKS_AGENT``, etc.) reaches them
    through login-shell sourcing of the profile fragment written earlier in
    phase 2.
    """
    from agentworks.resources.access import kind_dict
    from agentworks.vms.initializer.packages import _run_install_commands

    # DRIFT GUARD (Phase 7, recipe use-gate): this runner consumes
    # user-install-command rows by name and must never run a disabled plugin's
    # row. It is reached only via ``create_agent_on_vm``; the recipe gate lives
    # at each command entry that reaches it (see
    # tests/agents/test_recipe_gate_drift.py's entry table).
    path_additions = _run_install_commands(
        agent_files,
        agent_tmpl.user_install_commands,
        kind_dict(registry, "user-install-command"),
        agent_tmpl.shell,
        home,
        logger,
        label="Agent install command",
    )
    if agent_tmpl.mise_packages or agent_tmpl.mise_lockfile:
        path_additions.append(f"{home}/.local/share/mise/shims")
    _write_agentworks_profile(
        agent_files, path_additions, logger, home=home, shell=agent_tmpl.shell, identity_env=identity_env
    )


def _run_agent_mise_setup(
    *,
    agent_target: Transport,
    agent_files: Files,
    agent_tmpl: ResolvedAgentTemplate,
    home: str,
) -> None:
    """Set up mise for an agent: shims PATH, config, lockfile, install.

    Runs entirely over agent SSH. Writes the mise config and
    rc files directly via ``agent_target.write_file``; fetches the
    lockfile via ``fetch_file`` over the same agent transport so the
    file lands at its final path owned by the agent with no sudo step.

    ``mise install`` / ``mise prune`` are wrapped in a login shell
    (``{shell} -lc``) so the agent's PATH and any other profile-exported
    env (mise's own activation hooks, plugin discovery paths, downstream
    tooling like ``npm`` / ``pip`` that mise plugins shell out to during
    install) are in scope. No env= injection: provisioning is hermetic.
    """
    import shlex

    from agentworks.ssh import SSHError

    agent_cfg = agent_tmpl
    agent_shell = agent_cfg.shell
    has_packages = bool(agent_cfg.mise_packages)
    has_lockfile = bool(agent_cfg.mise_lockfile)

    if not has_packages and not has_lockfile:
        return

    mise_config_dir = f"{home}/.config/mise"

    # Write mise config if packages declared
    if has_packages:
        output.info(f"Writing mise config for agent ({len(agent_cfg.mise_packages)} packages)...")
        settings_lines = ["[settings]", f'install_before = "{agent_cfg.mise_install_before}"', ""]
        tools_lines = ["[tools]"]
        for pkg in agent_cfg.mise_packages:
            if "@" in pkg:
                name, version = pkg.rsplit("@", 1)
                tools_lines.append(f'"{name}" = "{version}"')
            else:
                tools_lines.append(f'"{pkg}" = "latest"')
        mise_config = "\n".join(settings_lines + tools_lines) + "\n"
        try:
            agent_target.run(f"mkdir -p {mise_config_dir}")
            agent_target.write_file(f"{mise_config_dir}/config.toml", mise_config, mode="0644")
        except SSHError as e:
            output.warn(f"agent mise config write failed: {e}")
            return

    # Copy lockfile if configured
    if has_lockfile and agent_cfg.mise_lockfile:
        output.info(f"Fetching agent mise lockfile from {agent_cfg.mise_lockfile}...")
        try:
            from agentworks.sources import fetch_file, parse_source_ref

            ref = parse_source_ref(agent_cfg.mise_lockfile, default_filename="mise.lock")
            # Published as the agent, which owns the destination.
            fetch_file(ref, agent_files, f"{mise_config_dir}/mise.lock")
        except STEP_FAILURES as error:
            output.warn(f"agent mise lockfile fetch failed: {error}")

    # Run mise install as the agent user
    lockfile_exists = False
    try:
        result = agent_target.run(f"test -f {mise_config_dir}/mise.lock", check=False)
        lockfile_exists = result.ok
    except SSHError:
        pass

    installed = False
    install_flags = "-y --locked" if lockfile_exists else "-y"
    try:
        agent_target.run(
            f"{agent_shell} -lc {shlex.quote(f'mise install {install_flags}')}",
            timeout=300,
        )
        output.info("Agent mise packages installed")
        installed = True
    except SSHError as e:
        if lockfile_exists and agent_cfg.mise_allow_unlocked:
            output.warn("some agent packages not in lockfile, installing unlocked...")
            try:
                agent_target.run(
                    f"{agent_shell} -lc {shlex.quote('mise install -y')}",
                    timeout=300,
                )
                output.info("Agent mise packages installed (unlocked)")
                installed = True
            except SSHError as e2:
                output.warn(f"agent mise install failed: {e2}")
        else:
            output.warn(f"agent mise install failed: {e}")
            if lockfile_exists:
                output.warn("set mise_allow_unlocked = true to install unlocked packages")

    # Prune stale tool versions not in the current config
    if installed and agent_cfg.mise_prune_on_reinit:
        import contextlib

        from agentworks.ssh import SSHError as _SSHError

        with contextlib.suppress(_SSHError):
            agent_target.run(
                f"{agent_shell} -lc {shlex.quote('mise prune -y')}",
                timeout=60,
            )
