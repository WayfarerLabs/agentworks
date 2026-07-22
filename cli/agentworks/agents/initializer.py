"""On-VM agent provisioning: the SSH bodies that create, configure,
and remove an agent's Linux user.

The agent counterpart of ``vms/initializer.py``. Command orchestration
(validation, gating, DB rows, rollback choreography) lives in
``agents/manager.py`` and ``agents/realize.py``; this module owns the
remote mutations they drive over SSH: the user bootstrap and
self-configure sequence, its install-command / profile / rc / mise
sub-steps, and the user removal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.transports import transport

if TYPE_CHECKING:
    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.install_commands import UserInstallCommandEntry
    from agentworks.resources import Registry
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport


def create_agent_on_vm(
    vm: VMRow,
    config: Config,
    registry: Registry,
    agent_tmpl: ResolvedAgentTemplate,
    linux_user: str,
    *,
    agent_name: str,
    git_tokens: dict[str, str],
    logger: SSHLogger,
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
       mise, claude plugins. The agent owns its home, so no sudo or
       cross-uid file writes are needed in this phase.

    Keeping these two phases disjoint by transport (admin_target vs.
    agent_target) minimizes the code surface that runs as root on the
    agent's behalf and matches the rule that operations whose target
    user is the agent open SSH directly as the agent's Linux user.
    """
    from agentworks.sessions.tmux import (
        cleanup_stale_sockets,
        ensure_agent_socket_dir,
        ensure_agent_socket_root,
    )
    from agentworks.transports import transport_for_user
    from agentworks.vms.initializer import _reconcile_authorized_keys

    admin_target = transport(vm, config, logger=logger)

    output.info(f"Creating user '{linux_user}' on VM '{vm.name}'...")
    home = f"/home/{linux_user}"

    agent_cfg = agent_tmpl
    agent_shell = agent_cfg.shell

    # -- Phase 1: bootstrap (admin) ---------------------------------------

    # Create user with the template's shell (idempotent: skip if exists).
    shell_path = f"/bin/{agent_shell}" if "/" not in agent_shell else agent_shell
    user_exists = admin_target.run(f"id {linux_user}", sudo=True, check=False)
    if not user_exists.ok:
        admin_target.run(f"useradd -m -s {shell_path} {linux_user}", sudo=True)
    else:
        admin_target.run(f"usermod -s {shell_path} {linux_user}", sudo=True)

    # Tmux socket infrastructure for the agent (root-owned ``/var/lib/``
    # parent; admin is the only transport that can create it).
    # ensure_agent_socket_root first so this works on VMs that haven't
    # been reinited since the socket feature was added. The per-agent
    # dir won't exist for a brand-new agent, so we suppress the
    # "missing" warning; misconfiguration of an existing dir still warns.
    ensure_agent_socket_root(admin_target, vm.admin_username)
    ensure_agent_socket_dir(admin_target, linux_user, warn_if_missing=False)
    removed = cleanup_stale_sockets(admin_target, linux_user)
    if removed:
        output.detail(f"Cleaned up {output.count(removed, 'stale socket')}")

    # Reconcile authorized_keys via stage-and-install. The only admin work
    # that lands content INTO the agent's home; everything below is the
    # agent writing to its own home over its own SSH session.
    _reconcile_authorized_keys(
        admin_target,
        config,
        home=home,
        logger=logger,
        owner=linux_user,
    )

    # -- Phase 2: self-configure (agent) ----------------------------------

    agent_target = transport_for_user(vm, config, user=linux_user, logger=logger)

    # Provisioning is hermetic: no operator env from [agent_templates.*.env]
    # or [vm_templates.*.env] is injected into the agent's install runners.
    # Static identity (AGENTWORKS_VM via /etc/profile.d/, AGENTWORKS_AGENT
    # via the per-user ~/.agentworks-profile.sh we write BELOW before the
    # install commands run) reaches the runners through login-shell
    # sourcing. Operator env only lands at runtime shells.

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
    _write_agent_profile(
        agent_target,
        home=home,
        shell=agent_cfg.shell,
        identity_env=agent_identity,
    )

    # Always write ~/.agentworks-rc.sh -- even when there are no shell
    # hooks to install. The defensive ``_ensure_agentworks_files_sourced``
    # step at the end of setup adds a ``. ~/.agentworks-rc.sh`` line to
    # the agent's .bashrc/.zshrc unconditionally; if the file doesn't
    # exist, every interactive login hits "No such file or directory".
    # Matches the admin pattern in vms/initializer.py:_write_agentworks_rc.
    _write_agent_shell_rc(agent_target, home=home, agent_cfg=agent_cfg)

    # No PS1 setup: operators who want an agent indicator can read
    # $AGENTWORKS_AGENT (exported by the per-user profile fragment we
    # just wrote) from their own prompt. A hardcoded PS1 lost against
    # starship / powerlevel10k anyway, and clobbered symlinked dotfiles
    # via scp.

    # Git safe.directory wildcard (agents access repos owned by admin).
    # Resolve the VM's own admin-template (NULL column = reserved
    # ``default``): agents on a VM provisioned from a non-default
    # admin-template must honor that template's git_force_safe_directory,
    # the same value the admin user resolved at provisioning.
    from agentworks.resources.access import admin_template as _admin_template

    if _admin_template(registry, vm.admin_template or "default").git_force_safe_directory:
        try:
            agent_target.run("git config --global --add safe.directory '*'")
            output.info("Git safe.directory configured for agent")
        except Exception as e:
            output.warn(f"agent git safe.directory setup failed: {e}")

    # Git credentials for the agent (tokens pre-resolved at the
    # caller's boundary and read through scoped delivery off the
    # credential nodes). The invariant: if the agent template declares
    # git_credentials, the caller MUST have resolved every token; a
    # missing entry is a caller bug and raises loudly rather than
    # shipping a VM with a silently-dropped credential the operator
    # asked for.
    if agent_cfg.git_credentials:
        from agentworks.vms.initializer import resolve_git_credential_providers

        output.info("Configuring git credentials...")
        providers = resolve_git_credential_providers(registry, agent_cfg.git_credentials)
        missing = [cred_name for cred_name in providers if cred_name not in git_tokens]
        if missing:
            from agentworks.errors import StateError

            raise StateError(
                f"agent git credential setup: token(s) not resolved by "
                f"the framework for {missing!r}; caller must pre-resolve "
                f"every provider's token before invoking this function",
                entity_kind="git-credential",
                entity_name=missing[0],
            )
        from agentworks.git_credentials import (
            GIT_CRED_HELPER_PATH,
            GIT_SCOPES_INCLUDE_PATH,
            build_credential_materials,
            runup_and_filter,
        )

        # Deferred git-credential runup, right before the write: a
        # rejected token is skipped (warned) and the rest are configured,
        # so a bad credential does not sink the agent's whole setup.
        providers = runup_and_filter(providers, git_tokens, config, logger)

        # Same materials as the VM-level (admin) flow: store lines with
        # the unscoped-first ordering contract, the gitconfig include
        # (just useHttpPath = true), and the selecting credential helper
        # (its get op serves credentials; erase only diagnoses).
        # ``git config --global`` runs as the agent user, so the
        # tilde-literal include.path resolves to the agent's home; the
        # write_file paths spell the home out (agent_target conventions).
        if providers:
            materials = build_credential_materials(providers, git_tokens)
            agent_target.write_file(f"{home}/.git-credentials", materials.store_content, mode="600")
            agent_target.write_file(
                f"{home}/{GIT_SCOPES_INCLUDE_PATH.removeprefix('~/')}",
                materials.gitconfig_content,
                mode="600",
            )
            agent_target.write_file(
                f"{home}/{GIT_CRED_HELPER_PATH.removeprefix('~/')}",
                materials.helper_script,
                mode="700",
            )
            agent_target.run(
                f"git config --global --replace-all credential.helper '!{GIT_CRED_HELPER_PATH}' && "
                f"(git config --global --get-all include.path | grep -qxF '{GIT_SCOPES_INCLUDE_PATH}' "
                f"|| git config --global --add include.path '{GIT_SCOPES_INCLUDE_PATH}')"
            )
            output.detail(f"Git credentials configured for {output.count(len(providers), 'provider')}")

    # User install commands + login-shell PATH profile fragment.
    _run_agent_install_commands(
        agent_target=agent_target,
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

            from agentworks.sources import SourceRefError, fetch_dir, parse_source_ref

            ref = parse_source_ref(agent_cfg.dotfiles_source)
            dest = agent_cfg.dotfiles_destination.replace("~", home)

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
                fetch_dir(ref, agent_target, dest)

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
    _run_agent_mise_setup(agent_target=agent_target, agent_tmpl=agent_tmpl, home=home)

    # Claude Code marketplaces and plugins. The probe (`command -v
    # claude`) and the actual `claude plugin ...` invocations need the
    # agent's PATH (mise shims, ~/.local/bin, etc.); a plain SSH command
    # gets a non-interactive non-login shell that sources none of the
    # rc / profile files. Wrap in `<shell> -lc` for parity with the
    # admin caller in vms/initializer.py.
    import shlex as _shlex

    from agentworks.vms.initializer import install_claude_plugins

    def _agent_run_cmd(cmd: str, timeout: int) -> object:
        return agent_target.run(
            f"{agent_shell} -lc {_shlex.quote(cmd)}",
            timeout=timeout,
        )

    install_claude_plugins(
        _agent_run_cmd,
        agent_cfg.claude_marketplaces,
        agent_cfg.claude_plugins,
    )

    # Defensive final step: re-ensure source lines in case dotfiles
    # install (or any other later step) overwrote a shell rc file in
    # place. Idempotent grep-or-append.
    from agentworks.vms.initializer import _ensure_agentworks_files_sourced

    _ensure_agentworks_files_sourced(
        agent_target,
        home=home,
        shell=agent_shell,
        logger=logger,
    )


def delete_agent_on_vm(
    vm: VMRow,
    config: Config,
    linux_user: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Remove an agent Linux user from a VM."""
    from agentworks.ssh import SSHError

    target = transport(vm, config, logger=logger)

    try:
        # Kill any running processes for the user
        target.run(f"pkill -u {linux_user}", sudo=True, check=False)
        # Remove the user and their home directory
        target.run(f"userdel -r {linux_user}", sudo=True)
    except SSHError as e:
        output.warn(f"remote cleanup for '{linux_user}' failed: {e}")


def _run_agent_install_commands(
    *,
    agent_target: Transport,
    registry: Registry,
    agent_tmpl: ResolvedAgentTemplate,
    home: str,
    identity_env: dict[str, str],
) -> None:
    """Run user install commands for an agent and rewrite the agent's
    profile fragment with the accumulated PATH. Failures warn but do
    not abort.

    Runs entirely over agent SSH. The agent owns its home, so
    the profile fragment is written via ``agent_target.write_file``
    directly, with no sudo / chown dance.

    The profile fragment is rewritten unconditionally (even when there
    are no install commands and no PATH additions) so that reinit can
    clear previously set paths. Install commands add their ``path``
    entries on top.

    Install commands run without any env= injection -- provisioning is
    hermetic. Static identity (``AGENTWORKS_AGENT``, etc.) reaches the
    install command via login-shell sourcing of the per-user profile
    fragment that was written earlier in agent-setup phase 2.
    """
    import shlex

    from agentworks.resources.access import kind_dict
    from agentworks.ssh import SSHError

    user_install_commands = kind_dict(registry, "user-install-command")
    shell = agent_tmpl.shell
    path_additions: list[str] = []
    command_names = agent_tmpl.user_install_commands
    total = len(command_names)

    for i, name in enumerate(command_names, 1):
        entry = user_install_commands.get(name)
        if entry is None:
            output.warn(f"'{name}' is not a declared user install command")
            continue
        # Skip if already installed for this user (short timeout)
        test_cmd = _build_agent_test_command(entry, home, shell)
        if test_cmd:
            try:
                check = agent_target.run(test_cmd, check=False, timeout=10)
                if check.returncode == 0:
                    output.info(f"Agent install command {i}/{total} ({name}): already installed, skipping")
                    path_additions.extend(entry.path)
                    continue
            except SSHError as e:
                output.warn(f"install check for '{name}' failed ({e}), assuming not installed")

        truncated = entry.command[:60]
        output.info(f"Agent install command {i}/{total} ({name}): {truncated}...")
        try:
            # Run the install command in a login shell to source the agent's
            # profile (provides AGENTWORKS_AGENT + PATH adds from earlier).
            agent_target.run(
                f"{shell} -lc {shlex.quote(entry.command)}",
                timeout=120,
            )
        except SSHError as e:
            output.warn(f"agent install command '{name}' failed: {e}")
        path_additions.extend(entry.path)

    # Rewrite the agent's profile fragment with identity + accumulated
    # PATH additions. The fragment was written with identity-only earlier
    # in create_agent_on_vm (so install commands above could see
    # AGENTWORKS_AGENT via login-shell sourcing); the rewrite here adds
    # the PATH entries those install commands contributed.
    if path_additions:
        output.info(f"Adding {len(path_additions)} PATH entries for agent...")
    _write_agent_profile(
        agent_target,
        home=home,
        shell=shell,
        identity_env=identity_env,
        path_additions=path_additions,
    )


def _write_agent_profile(
    agent_target: Transport,
    *,
    home: str,
    shell: str,
    identity_env: dict[str, str],
    path_additions: list[str] | None = None,
) -> None:
    """Write the agent's ``~/.agentworks-profile.sh`` and source it from
    the agent's shell rc files.

    Used twice in agent setup: once with identity-only (before install
    commands run, so they see AGENTWORKS_AGENT via login-shell sourcing),
    and once with identity + accumulated PATH (after install commands).
    Both writes overwrite the file; the source-line append is
    grep-or-append so the rc files don't accumulate duplicate lines on
    reinit.
    """
    import shlex

    from agentworks.ssh import SSHError
    from agentworks.vms.initializer import AGENTWORKS_PROFILE

    lines = ["# Managed by agentworks -- do not edit"]
    for key, value in identity_env.items():
        lines.append(f"export {key}={shlex.quote(value)}")
    for p in path_additions or []:
        expanded = p.replace("~", "$HOME", 1) if p.startswith("~") else p
        lines.append(f'export PATH="{expanded}:$PATH"')
    content = "\n".join(lines) + "\n"
    try:
        profile_path = f"{home}/{AGENTWORKS_PROFILE}"
        agent_target.write_file(profile_path, content, mode="0644")
        source_line = f". {profile_path}"
        rc_files = [f"{home}/.profile", f"{home}/.bashrc"]
        if shell == "zsh":
            rc_files.append(f"{home}/.zprofile")
        for rc in rc_files:
            agent_target.run(
                f"grep -q {AGENTWORKS_PROFILE} {rc} 2>/dev/null || printf '%s\\n' '{source_line}' >> {rc}",
            )
    except SSHError as e:
        output.warn(f"agent profile configuration failed: {e}")


def _write_agent_shell_rc(
    agent_target: Transport,
    *,
    home: str,
    agent_cfg: ResolvedAgentTemplate,
) -> None:
    """Write the agent's ``~/.agentworks-rc.sh`` and source it from the
    agent's shell rc files.

    Called unconditionally from agent setup so the source line added by
    :func:`agentworks.vms.initializer._ensure_agentworks_files_sourced`
    always points at an existing file. Matches the admin pattern in
    :func:`agentworks.vms.initializer._write_agentworks_rc`: a placeholder
    body when there's no shell hook to install, the mise-activate hook
    when one is configured.
    """
    from agentworks.ssh import SSHError
    from agentworks.vms.initializer import AGENTWORKS_RC, MISE_ACTIVATE_LINES

    snippet = MISE_ACTIVATE_LINES if agent_cfg.mise_activate else "# mise activation disabled"
    content = f"# Managed by agentworks -- do not edit\n{snippet}\n"
    try:
        rc_path = f"{home}/{AGENTWORKS_RC}"
        agent_target.write_file(rc_path, content, mode="0644")
        source_line = f". {rc_path}"
        for rc in [f"{home}/.bashrc", f"{home}/.zshrc"]:
            agent_target.run(
                f"grep -q {AGENTWORKS_RC} {rc} 2>/dev/null || printf '%s\\n' '{source_line}' >> {rc}",
            )
    except SSHError as e:
        output.warn(f"agent rc configuration failed: {e}")


def _run_agent_mise_setup(
    *,
    agent_target: Transport,
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

    from agentworks.vms.initializer import AGENTWORKS_PROFILE

    # Append mise shims PATH to agent's agentworks profile
    shims_path = f"{home}/.local/share/mise/shims"
    try:
        profile_path = f"{home}/{AGENTWORKS_PROFILE}"
        agent_target.run(
            f"printf '%s' 'export PATH=\"{shims_path}:$PATH\"\n' >> {profile_path}",
        )
        source_line = f". {profile_path}"
        for rc in [f"{home}/.profile", f"{home}/.zprofile"]:
            agent_target.run(
                f"grep -q {AGENTWORKS_PROFILE} {rc} 2>/dev/null || printf '%s\\n' '{source_line}' >> {rc}",
            )
    except SSHError as e:
        output.warn(f"agent profile configuration failed: {e}")

    # ``~/.agentworks-rc.sh`` is written unconditionally by
    # ``_write_agent_shell_rc`` earlier in setup; nothing more to do here.

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
            from agentworks.sources import SourceRefError, fetch_file, parse_source_ref

            ref = parse_source_ref(agent_cfg.mise_lockfile, default_filename="mise.lock")
            agent_target.run(f"mkdir -p {mise_config_dir}")
            # Fetch directly into the agent's config dir over agent SSH;
            # the agent owns the destination so no sudo / chown needed.
            fetch_file(ref, agent_target, f"{mise_config_dir}/mise.lock")
        except (SourceRefError, SSHError) as e:
            output.warn(f"agent mise lockfile fetch failed: {e}")

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


def _build_agent_test_command(
    entry: UserInstallCommandEntry,
    home: str,
    shell: str,
) -> str | None:
    """Build a test command that runs as the agent user.

    The caller runs this via the agent's ``Transport``. ``test_exec`` checks
    are wrapped in a login shell so the agent's PATH (including mise shims
    and ~/.local/bin) is in scope; ``test_file`` / ``test_dir`` use plain
    POSIX tests against absolute paths in the agent's home.
    """
    import shlex as _shlex

    test_exec: str | None = getattr(entry, "test_exec", None)
    test_file: str | None = getattr(entry, "test_file", None)
    test_dir: str | None = getattr(entry, "test_dir", None)
    if test_exec:
        inner = f"command -v {_shlex.quote(test_exec)} > /dev/null 2>&1"
        return f"{shell} -lc {_shlex.quote(inner)}"
    if test_file:
        path = test_file.replace("~", home, 1) if test_file.startswith("~") else test_file
        return f"test -f {_shlex.quote(path)}"
    if test_dir:
        path = test_dir.replace("~", home, 1) if test_dir.startswith("~") else test_dir
        return f"test -d {_shlex.quote(path)}"
    return None
