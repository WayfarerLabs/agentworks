"""On-VM agent provisioning: the requests that create, configure, and remove an agent's Linux user.

The agent counterpart of ``agentworks.vms.initializer``. Command orchestration
(validation, gating, DB rows, rollback choreography) lives in
``agentworks.agents.manager`` and ``agents/realize.py``; this module owns the
guest mutations they drive, on the admin's and the agent's ``VMAccess``: the
user bootstrap and self-configure sequence, its install-command / profile /
rc / mise sub-steps, and the user removal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.execution as execution
from agentworks import output
from agentworks.execution.access import Admin, Agent
from agentworks.execution.requests import Command, Script, Shell
from agentworks.git_config import ensure_safe_directory_wildcard
from agentworks.linux_users import managed_home
from agentworks.vms.home_layout import MANAGED
from agentworks.vms.initializer.dotfiles import sync_dotfiles
from agentworks.vms.initializer.mise import MISE_ACTIVATE_LINES, _write_mise_config, install_mise
from agentworks.vms.initializer.packages import _run_install_commands, install_command_paths
from agentworks.vms.initializer.shell_env import (
    _ensure_agentworks_files_sourced,
    _write_agentworks_profile,
    _write_agentworks_rc,
)
from agentworks.vms.initializer.steps import STEP_FAILURES, describe_failure, status

if TYPE_CHECKING:
    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.execution.requests import AccountShell
    from agentworks.git_credentials import CredentialRequest
    from agentworks.resources import Registry
    from agentworks.vms.initializer.steps import InitLog


def create_new_agent_user(
    vm: VMRow,
    config: Config,
    linux_user: str,
    *,
    shell: AccountShell,
    logger: InitLog,
) -> None:
    """Establish fresh ownership before the caller may arm user rollback.

    Refuse an unowned account or home, including dangling symlinks. A competing
    useradd fails exclusively; the caller must not delete anything on that failure.
    Existing DB-owned agents instead use create_agent_on_vm's convergence path.
    """
    import shlex

    from agentworks.errors import StateError

    admin = execution.vm_access(vm, config, Admin(), log=logger.record)
    home = managed_home(linux_user)
    account = Command(("getent", "passwd", linux_user), elevated=True)
    home_entry = Script(f"test -e {shlex.quote(home)} || test -L {shlex.quote(home)}", Shell.SH, elevated=True)
    if status(admin, account) == 0 or status(admin, home_entry) == 0:
        raise StateError(
            f"cannot create agent: unowned user or home already exists for '{linux_user}'",
            entity_kind="agent",
            entity_name=linux_user,
            hint="Choose another agent name or remove the unowned native account and home first.",
        )
    admin.run(Command(("useradd", "-m", "-U", "-s", f"/bin/{shell}", linux_user), elevated=True), check=True)


def create_agent_on_vm(
    vm: VMRow,
    config: Config,
    registry: Registry,
    agent_tmpl: ResolvedAgentTemplate,
    linux_user: str,
    *,
    agent_name: str,
    credential_requests: tuple[CredentialRequest, ...],
    logger: InitLog,
    admin_git_force_safe_directory: bool,
) -> None:
    """Create an agent Linux user on a VM and configure their environment.

    Workspace group membership is NOT set here; it is managed by the
    grant system. This function only creates the user and configures
    their tools.

    The work splits cleanly into two phases by who is running each step:

    1. **Bootstrap (admin)**: ``useradd`` / ``usermod``, the home's mode
       and group check, the tmux socket directories under ``/run``, and
       ``authorized_keys``, all as root on the admin's access. The agent's
       ``.ssh`` and ``authorized_keys`` are placed by the file program with
       the agent as owner, through descriptors opened without following a
       link, before the file is published. That last step is what makes the
       agent's own access work from this point onward. This is the only
       admin work in agent create / reinit, and a failure in it raises.
    2. **Self-configure (agent)**: every subsequent step runs on the
       agent's own access, which refuses any elevated request before
       dispatch. In order: the profile and rc fragments and their source
       lines, the mise config, git config + credentials, dotfiles, the
       mise lockfile and install, the source lines again, the user install
       commands, and a final pass over the source lines; the admin's order.
       The agent owns its home, so no root or cross-uid writes are needed
       in this phase, and a failed step warns.

    Keeping these two phases disjoint by access minimizes the code surface
    that runs as root on the agent's behalf and matches the rule that
    operations whose target user is the agent run as the agent's Linux user.
    """
    # DRIFT GUARD (Phase 7, recipe use-gate): this function is itself never
    # gated; it runs ``_run_install_commands`` (a declarable consumer), so
    # the recipe gate (``ensure_recipe_enabled``) must sit at each COMMAND ENTRY
    # that reaches it (agent create/reinit, session create --new-agent). If you
    # add a NEW caller of ``create_agent_on_vm``, add its command-entry gate and
    # update tests/agents/test_recipe_gate_drift.py's enumerated caller set.
    from agentworks.sessions.tmux import (
        cleanup_stale_sockets,
        ensure_agent_socket_dir,
        ensure_agent_socket_root,
    )
    from agentworks.vms.initializer import _reconcile_authorized_keys

    admin = execution.vm_access(vm, config, Admin(), log=logger.record)
    admin_files = admin.files()

    home = managed_home(linux_user)

    agent_shell = agent_tmpl.shell

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

    agent_access = execution.vm_access(vm, config, Agent(agent_name, linux_user), log=logger.record)
    agent_files = agent_access.files()

    # Provisioning is hermetic: no operator env from [agent_templates.*.env]
    # or [vm_templates.*.env] is injected into the agent's install runners.
    # Static identity (AGENTWORKS_VM via /etc/profile.d/, AGENTWORKS_AGENT
    # via the per-user ~/.agentworks-profile.sh written below, before the
    # dotfiles installer, mise and the install commands run) reaches them
    # through login-shell sourcing. Integration setup receives operator env
    # through its own runner.

    # Write the agent's per-user profile fragment first, as the admin's is, so
    # that AGENTWORKS_AGENT and the declared PATH entries reach every later
    # login shell: mise's shims, with the user install commands' own paths
    # ahead of them on PATH. The entries are declarations, written in full
    # here so every install command sees them, which lets reinit clear
    # previously set paths; a command that fails is dropped after the batch.
    from agentworks.env import ResourceContext, per_user_identity_env
    from agentworks.resources.access import kind_dict
    from agentworks.vms.initializer.mise import _mise_shims_path
    from agentworks.vms.sites import site_platform_name

    agent_identity_ctx = ResourceContext(
        vm_name=vm.name,
        platform=site_platform_name(vm.site, registry),
        site=vm.site,
        user=linux_user,
        agent_name=agent_name,
    )
    agent_identity = per_user_identity_env(agent_identity_ctx)
    user_install_commands = kind_dict(registry, "user-install-command")

    def write_profile(failed: list[str]) -> None:
        paths = _mise_shims_path(home) + install_command_paths(
            [name for name in agent_tmpl.user_install_commands if name not in failed], user_install_commands
        )
        _write_agentworks_profile(agent_files, paths, logger, home=home, shell=agent_shell, identity_env=agent_identity)

    write_profile([])

    # Always write ~/.agentworks-rc.sh, even when there are no shell hooks to
    # install: the writer and both ``_ensure_agentworks_files_sourced`` passes
    # below add a ``. ~/.agentworks-rc.sh`` line to the agent's .bashrc/.zshrc
    # unconditionally, so a missing file would break every interactive login.
    snippet = MISE_ACTIVATE_LINES if agent_tmpl.mise_activate else "# mise activation disabled"
    _write_agentworks_rc(agent_files, [snippet], logger, home=home, shell=agent_shell)

    # No PS1 setup: operators who want an agent indicator can read
    # $AGENTWORKS_AGENT (exported by the per-user profile fragment we
    # just wrote) from their own prompt. A hardcoded PS1 lost against
    # starship / powerlevel10k anyway, and clobbered symlinked dotfiles
    # via scp.

    # The admin's order from here on (docs/guides/mise.md): the mise config,
    # git credentials, dotfiles, the mise lockfile and install, the source
    # lines again, the user install commands, which may depend on mise's
    # tools, and a last pass over the source lines. Dotfiles may override the
    # config, and an explicit lockfile overrides the dotfiles'.
    if agent_tmpl.mise_packages:
        _write_mise_config(agent_files, agent_tmpl.mise_packages, agent_tmpl.mise_install_before, home, logger)

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

    # Dotfiles: the same sync as the admin's, as the agent.
    if agent_tmpl.dotfiles_source:
        sync_dotfiles(
            agent_files,
            source=agent_tmpl.dotfiles_source,
            destination=agent_tmpl.dotfiles_destination,
            install_cmd=agent_tmpl.dotfiles_install_cmd,
            shell=agent_shell,
            home=home,
            managed=MANAGED,
            log=logger,
        )

    install_mise(
        agent_files,
        packages=agent_tmpl.mise_packages,
        lockfile=agent_tmpl.mise_lockfile,
        allow_unlocked=agent_tmpl.mise_allow_unlocked,
        prune=agent_tmpl.mise_prune_on_reinit,
        shell=agent_shell,
        home=home,
        log=logger,
    )

    # A dotfiles installer can replace an rc file, so the fragments' source
    # lines are ensured again before the install commands' login shells.
    _ensure_agentworks_files_sourced(agent_files, logger, home=home, shell=agent_shell)

    # User install commands, as the agent, reading the profile fragment written
    # above. DRIFT GUARD (Phase 7, recipe use-gate): this runner consumes
    # user-install-command rows by name and must never run a disabled plugin's
    # row; the recipe gate lives at each command entry that reaches this
    # function (tests/agents/test_recipe_gate_drift.py's entry table).
    failed = _run_install_commands(
        agent_files,
        agent_tmpl.user_install_commands,
        user_install_commands,
        agent_shell,
        home,
        logger,
        label="Agent install command",
    )
    if failed:
        # A failed install command keeps no PATH entry in the profile.
        write_profile(failed)

    # Last pass: an install command can replace a shell rc file too. A file
    # that already names a fragment is left alone.
    _ensure_agentworks_files_sourced(agent_files, logger, home=home, shell=agent_shell)


def delete_agent_on_vm(
    vm: VMRow,
    config: Config,
    linux_user: str,
    *,
    logger: InitLog | None = None,
) -> None:
    """Remove an agent Linux user from a VM, along with related on-VM
    state: their processes are killed, the user and home directory are
    removed, and the per-agent tmux socket directory is cleaned up.

    Runs as root on the admin's access; a step failure warns.
    """
    from agentworks.sessions.tmux import agent_socket_dir

    admin = execution.vm_access(vm, config, Admin(), log=None if logger is None else logger.record)
    try:
        # Kill any running processes for the user. This also frees any live
        # tmux sockets under the per-agent socket dir removed next.
        admin.run(Command(("pkill", "-u", linux_user), elevated=True)).require_completed(vm.name)
        # Remove the per-agent tmux socket directory (created by
        # ``ensure_agent_socket_dir`` at create time). It lives on tmpfs
        # under /run, is owned by the agent uid, and would otherwise linger
        # until reboot. Done BEFORE userdel and best-effort (its status is
        # not read) so that a userdel failure (e.g. exit 12, home or
        # mail-spool cleanup) cannot skip it and re-orphan the dir under the
        # freed uid. The dir is separate from the home directory userdel
        # removes, and the sockets are already dead from the pkill above, so
        # this ordering is safe.
        admin.run(Command(("rm", "-rf", "--", agent_socket_dir(linux_user)), elevated=True)).require_completed(vm.name)
        # Remove the user and their home directory
        admin.run(Command(("userdel", "-r", linux_user), elevated=True), check=True)
    except STEP_FAILURES as error:
        sanitize = logger.sanitize if logger is not None else str
        output.warn(f"remote cleanup for '{linux_user}' failed: {describe_failure(error, sanitize)}")
