"""Agent lifecycle orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

from agentworks.config import validate_name
from agentworks.ssh import ssh_target_for_vm

if TYPE_CHECKING:
    from agentworks.catalog import UserInstallCommandEntry
    from agentworks.config import Config
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.ssh import SSHLogger, SSHResult

AGENT_SEPARATOR = "--"


def _run_as_agent(
    target: object,
    linux_user: str,
    command: str,
    *,
    check: bool = True,
    timeout: int | None = None,
    logger: SSHLogger | None = None,
) -> SSHResult:
    """Run a command as an agent user via su -.

    Uses su - for a login shell so the agent's environment is set up.
    """
    import shlex

    from agentworks.ssh import SSHResult, run_as_root

    inner = shlex.quote(command)
    result = run_as_root(
        target,
        f"su - {shlex.quote(linux_user)} -c {inner}",
        check=check,
        timeout=timeout,
        logger=logger,
    )
    assert isinstance(result, SSHResult)
    return result


def _write_agent_file(
    target: object,
    linux_user: str,
    dest: str,
    content: str,
    *,
    mode: str | None = None,
    logger: SSHLogger | None = None,
) -> None:
    """Write a file into an agent user's home via tmp + mv.

    scp runs as admin and can't write to the agent's home directly.
    """
    from agentworks.ssh import run_as_root, write_file

    safe_name = linux_user.replace("/", "-")
    tmp_path = f"/tmp/agentworks-{safe_name}-{dest.rsplit('/', 1)[-1]}"
    write_file(target, tmp_path, content, logger=logger)
    run_as_root(target, f"mv {tmp_path} {dest}", logger=logger)
    run_as_root(target, f"chown {linux_user}:{linux_user} {dest}", logger=logger)
    if mode:
        run_as_root(target, f"chmod {mode} {dest}", logger=logger)


def derive_linux_user(workspace_name: str, agent_name: str) -> str:
    """Derive the Linux username for an agent: <workspace>--<agent>."""
    return f"{workspace_name}{AGENT_SEPARATOR}{agent_name}"


def create_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
    template: str | None = None,
) -> None:
    """Create an agent on a workspace."""
    from dataclasses import replace as _replace

    from agentworks.agents.templates import resolve_template

    agent_tmpl = resolve_template(config, template)

    # Replace config.agent with the resolved template so downstream code uses it
    if template is not None:
        config = _replace(config, agent=agent_tmpl)

    validate_name(name)

    ws = _require_workspace(db, workspace_name)

    if db.get_agent(workspace_name, name) is not None:
        typer.echo(f"Error: agent '{name}' already exists in workspace '{workspace_name}'", err=True)
        raise typer.Exit(1)

    linux_user = derive_linux_user(workspace_name, name)

    if ws.type == "local":
        typer.echo("Error: agents are not supported on local workspaces", err=True)
        raise typer.Exit(1)

    if ws.type == "vm":
        vm = _require_vm_for_workspace(db, ws)
        from agentworks.ssh import SSHLogger

        ssh_logger = SSHLogger(vm.name, "agent-create")
        try:
            _create_agent_on_vm(vm, config, linux_user, workspace_name, logger=ssh_logger)
        except Exception as e:
            ssh_logger.close()
            typer.echo(f"Error creating agent: {e}", err=True)
            typer.echo(f"  SSH log: {ssh_logger.path}", err=True)
            typer.echo(f"  Cleaning up user '{linux_user}'...", err=True)
            _delete_agent_on_vm(vm, config, linux_user, logger=ssh_logger)
            raise typer.Exit(1) from None
        ssh_logger.close()

    agent = db.insert_agent(name, workspace_name, linux_user, template=agent_tmpl.name)

    typer.echo(f"Agent '{name}' created (user: {agent.linux_user})")


def delete_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
) -> None:
    """Delete an agent from a workspace."""
    ws = _require_workspace(db, workspace_name)
    agent = db.get_agent(workspace_name, name)
    if agent is None:
        typer.echo(f"Error: agent '{name}' not found in workspace '{workspace_name}'", err=True)
        raise typer.Exit(1)

    if ws.type == "vm":
        vm = _require_vm_for_workspace(db, ws)
        from agentworks.ssh import SSHLogger

        ssh_logger = SSHLogger(vm.name, "agent-delete")
        _delete_agent_on_vm(vm, config, agent.linux_user, logger=ssh_logger)
        ssh_logger.close()

    db.delete_agent(workspace_name, name)

    typer.echo(f"Agent '{name}' deleted from workspace '{workspace_name}'")


def reinit_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
) -> None:
    """Re-run agent setup using the stored template."""
    from dataclasses import replace as _replace

    from agentworks.agents.templates import resolve_template

    ws = _require_workspace(db, workspace_name)
    agent = db.get_agent(workspace_name, name)
    if agent is None:
        typer.echo(f"Error: agent '{name}' not found in workspace '{workspace_name}'", err=True)
        raise typer.Exit(1)

    if ws.type != "vm":
        typer.echo("Error: agents are only supported on VM workspaces", err=True)
        raise typer.Exit(1)

    # Resolve the agent's stored template
    agent_tmpl = resolve_template(config, agent.template)
    if agent.template and agent.template != "default":
        config = _replace(config, agent=agent_tmpl)

    vm = _require_vm_for_workspace(db, ws)

    from agentworks.ssh import SSHLogger

    ssh_logger = SSHLogger(vm.name, "agent-reinit")
    try:
        _create_agent_on_vm(vm, config, agent.linux_user, workspace_name, logger=ssh_logger)
    except Exception as e:
        ssh_logger.close()
        typer.echo(f"Error reinitializing agent: {e}", err=True)
        typer.echo(f"  SSH log: {ssh_logger.path}", err=True)
        raise typer.Exit(1) from None
    ssh_logger.close()

    typer.echo(f"Agent '{name}' reinitialized")


def delete_agents_for_workspace(
    db: Database,
    config: Config,
    ws: WorkspaceRow,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Delete all agents for a workspace (called during workspace deletion).

    Skips tmuxinator regeneration since the workspace itself is being deleted.
    """
    agents = db.delete_agents_for_workspace(ws.name)
    if not agents:
        return

    if ws.type == "vm" and ws.vm_name:
        vm = db.get_vm(ws.vm_name)
        if vm is not None:
            for agent in agents:
                _delete_agent_on_vm(vm, config, agent.linux_user, logger=logger)

    names = ", ".join(a.name for a in agents)
    typer.echo(f"  Deleted {len(agents)} agent(s): {names}")


def list_agents(
    db: Database,
    *,
    workspace_name: str | None = None,
) -> None:
    """List agents."""
    agents = db.list_agents(workspace_name=workspace_name)
    if not agents:
        typer.echo("No agents found.")
        return

    typer.echo(f"{'NAME':<20} {'WORKSPACE':<20} {'TEMPLATE':<12} {'LINUX USER':<30} {'CREATED'}")
    typer.echo("-" * 107)
    for agent in agents:
        typer.echo(
            f"{agent.name:<20} {agent.workspace_name:<20} {agent.template or '-':<12} "
            f"{agent.linux_user:<30} {agent.created_at}"
        )


def shell_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str,
) -> None:
    """Open a shell as an agent user on a VM."""
    ws = _require_workspace(db, workspace_name)
    agent = db.get_agent(workspace_name, name)
    if agent is None:
        typer.echo(f"Error: agent '{name}' not found in workspace '{workspace_name}'", err=True)
        raise typer.Exit(1)

    if ws.type != "vm":
        typer.echo("Error: agents are only supported on VM workspaces", err=True)
        raise typer.Exit(1)

    vm = _require_vm_for_workspace(db, ws)

    from agentworks.workspaces.manager import _ensure_vm_running

    _ensure_vm_running(db, config, vm)

    import sys

    from agentworks.ssh import interactive

    target = ssh_target_for_vm(vm, config)
    sys.exit(interactive(target, f"cd {ws.workspace_path} && exec sudo su - {agent.linux_user}"))


# -- VM operations ---------------------------------------------------------


def _create_agent_on_vm(
    vm: VMRow,
    config: Config,
    linux_user: str,
    workspace_name: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Create an agent Linux user on a VM and run user install commands."""
    from agentworks.ssh import run_as_root

    target = ssh_target_for_vm(vm, config)
    lg = logger

    typer.echo(f"  Creating user '{linux_user}' on VM '{vm.name}'...")
    home = f"/home/{linux_user}"
    ws_group = f"ws-{workspace_name}"
    ws_path = f"/home/{vm.admin_username}/workspaces/{workspace_name}"

    # Ensure the workspace group exists, admin is a member, and the
    # workspace directory has correct group ownership + setgid. This
    # repairs existing workspaces that were created before group support.
    run_as_root(target, f"sh -c 'getent group {ws_group} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_group}'", logger=lg)
    run_as_root(target, f"usermod -aG {ws_group} {vm.admin_username}", logger=lg)
    run_as_root(
        target,
        f"sh -c 'test -d {ws_path} && chgrp -R {ws_group} {ws_path} && chmod 2775 {ws_path}'",
        check=False,
        logger=lg,
    )

    agent_cfg = config.agent
    agent_shell = agent_cfg.shell

    # Create user with the template's shell (idempotent: skip if exists)
    shell_path = f"/bin/{agent_shell}" if "/" not in agent_shell else agent_shell
    user_exists = run_as_root(target, f"id {linux_user}", check=False, logger=lg)
    if not user_exists.ok:
        run_as_root(target, f"useradd -m -s {shell_path} {linux_user}", logger=lg)
    else:
        # Update shell in case template changed
        run_as_root(target, f"usermod -s {shell_path} {linux_user}", logger=lg)
    run_as_root(target, f"usermod -aG {ws_group} {linux_user}", logger=lg)

    # Write a minimal rc file with a clear agent prompt
    if agent_shell == "zsh":
        rc_content = f"export PS1='[agent:{linux_user}] %~%# '\n"
        rc_file = f"{home}/.zshrc"
    elif agent_shell == "bash":
        rc_content = f"export PS1='[agent:{linux_user}] \\w\\$ '\n"
        rc_file = f"{home}/.bashrc"
    else:
        typer.echo(f"  Warning: unsupported shell '{agent_shell}', skipping prompt configuration", err=True)
        rc_content = None
        rc_file = None

    if rc_content and rc_file:
        _write_agent_file(target, linux_user, rc_file, rc_content, logger=lg)

    # Git credentials for the agent
    if agent_cfg.git_credentials:
        from agentworks.vms.initializer import resolve_git_credential_providers

        typer.echo("  Configuring git credentials for agent...")
        try:
            providers = resolve_git_credential_providers(config, agent_cfg.git_credentials)
            cred_lines: list[str] = []
            for _cred_name, provider in providers.items():
                token = provider.obtain_token(vm.name)
                cred_lines.extend(provider.credential_lines(token))
            if cred_lines:
                cred_content = "\n".join(cred_lines) + "\n"
                _write_agent_file(target, linux_user, f"{home}/.git-credentials", cred_content, mode="600", logger=lg)
                _run_as_agent(target, linux_user, "git config --global credential.helper store", logger=lg)
        except Exception as e:
            typer.echo(f"  Warning: agent git credential setup failed: {e}", err=True)

    # User install commands for the agent
    _run_agent_install_commands(vm, config, linux_user, home)

    # Dotfiles for the agent
    if agent_cfg.dotfiles_source:
        typer.echo(f"  Syncing agent dotfiles from {agent_cfg.dotfiles_source}...")
        try:
            from agentworks.sources import SourceRefError, fetch_dir, parse_source_ref

            ref = parse_source_ref(agent_cfg.dotfiles_source)
            dest = agent_cfg.dotfiles_destination.replace("~", home)

            # Clone as the agent user (git credentials are already configured)
            if ref.kind == "git":
                clone_cmd = f"git clone {ref.path} {dest}"
                if ref.ref:
                    import shlex as _shlex

                    clone_cmd = f"git clone --branch {_shlex.quote(ref.ref)} {ref.path} {dest}"
                _run_as_agent(target, linux_user, clone_cmd, timeout=120, logger=lg)
            else:
                # Local source: copy as admin then chown
                from agentworks.ssh import ExecTarget

                exec_target = ExecTarget(ssh=ssh_target_for_vm(vm, config))
                tmp_dotfiles = f"/tmp/agentworks-{linux_user}-dotfiles"
                exec_target.run(f"rm -rf {tmp_dotfiles}", check=False)
                from agentworks.sources import fetch_dir

                fetch_dir(ref, exec_target, tmp_dotfiles)
                run_as_root(target, f"mv {tmp_dotfiles} {dest}", logger=lg)
                run_as_root(target, f"chown -R {linux_user}:{linux_user} {dest}", logger=lg)

            typer.echo(f"  Running agent dotfiles install: {agent_cfg.dotfiles_install_cmd}")
            _run_as_agent(
                target, linux_user,
                f"cd {dest} && {agent_cfg.dotfiles_install_cmd}",
                timeout=120, logger=lg,
            )
        except (SourceRefError, Exception) as e:
            typer.echo(f"  Warning: agent dotfiles failed: {e}", err=True)

    # Mise for the agent
    if config.vm.install_mise:
        _run_agent_mise_setup(vm, config, linux_user, home)


def _delete_agent_on_vm(
    vm: VMRow,
    config: Config,
    linux_user: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Remove an agent Linux user from a VM."""
    from agentworks.ssh import SSHError, run_as_root

    target = ssh_target_for_vm(vm, config)
    lg = logger

    try:
        # Kill any running processes for the user
        run_as_root(target, f"pkill -u {linux_user}", check=False, logger=lg)
        # Remove the user and their home directory
        run_as_root(target, f"userdel -r {linux_user}", logger=lg)
    except SSHError as e:
        typer.echo(f"Warning: remote cleanup for '{linux_user}' failed: {e}", err=True)


def _run_agent_install_commands(
    vm: VMRow,
    config: Config,
    linux_user: str,
    home: str,
) -> None:
    """Run user install commands for an agent. Failures warn but do not abort."""
    command_names = config.agent.user_install_commands
    if not command_names:
        return

    import shlex

    from agentworks.catalog import load_catalog
    from agentworks.ssh import SSHError, run_as_root

    catalog = load_catalog(config)
    target = ssh_target_for_vm(vm, config)
    shell = config.agent.shell
    path_additions: list[str] = []
    total = len(command_names)

    for i, name in enumerate(command_names, 1):
        entry = catalog.user_install_commands.get(name)
        if entry is None:
            typer.echo(f"  Warning: install command '{name}' not found in catalog", err=True)
            continue
        # Skip if already installed for this user (short timeout)
        test_cmd = _build_agent_test_command(entry, linux_user, home)
        if test_cmd:
            try:
                check = run_as_root(target, test_cmd, check=False, timeout=10)
                if check.returncode == 0:
                    typer.echo(f"  Agent install command {i}/{total} ({name}): already installed, skipping")
                    path_additions.extend(entry.path)
                    continue
            except SSHError as e:
                typer.echo(f"  Warning: install check for '{name}' failed ({e}), assuming not installed", err=True)

        truncated = entry.command[:60]
        typer.echo(f"  Agent install command {i}/{total} ({name}): {truncated}...")
        try:
            # Run as the agent user via su, in their login shell
            run_as_root(
                target,
                f"su - {shlex.quote(linux_user)} -c {shlex.quote(f'{shell} -lc {shlex.quote(entry.command)}')}",
                timeout=120,
            )
        except SSHError as e:
            typer.echo(f"  Warning: agent install command '{name}' failed: {e}", err=True)
        path_additions.extend(entry.path)

    # Write PATH additions for the agent
    if path_additions:
        from agentworks.vms.initializer import AGENTWORKS_PROFILE

        typer.echo(f"  Adding {len(path_additions)} PATH entries for agent...")
        lines = ["# Managed by agentworks -- do not edit"]
        for p in path_additions:
            expanded = p.replace("~", "$HOME", 1) if p.startswith("~") else p
            lines.append(f'export PATH="{expanded}:$PATH"')
        content = "\n".join(lines) + "\n"
        try:
            profile_path = f"{home}/{AGENTWORKS_PROFILE}"
            _write_agent_file(target, linux_user, profile_path, content)
            # Source from shell profiles (run as agent so appends work)
            source_line = f". {profile_path}"
            rc_files = [f"{home}/.profile", f"{home}/.bashrc"]
            if shell == "zsh":
                rc_files.append(f"{home}/.zprofile")
            for rc in rc_files:
                _run_as_agent(
                    target, linux_user,
                    f"grep -q {AGENTWORKS_PROFILE} {rc} 2>/dev/null"
                    f" || printf '%s\\n' '{source_line}' >> {rc}",
                )
        except SSHError as e:
            typer.echo(f"  Warning: agent PATH configuration failed: {e}", err=True)


def _run_agent_mise_setup(
    vm: VMRow,
    config: Config,
    linux_user: str,
    home: str,
) -> None:
    """Set up mise for an agent: shims PATH, config, lockfile, install."""
    from agentworks.ssh import SSHError, run_as_root

    target = ssh_target_for_vm(vm, config)
    agent_cfg = config.agent
    has_packages = bool(agent_cfg.mise_packages)
    has_lockfile = bool(agent_cfg.mise_lockfile)

    if not has_packages and not has_lockfile:
        return

    from agentworks.vms.initializer import AGENTWORKS_PROFILE, AGENTWORKS_RC, MISE_ACTIVATE_LINES

    # Append mise shims PATH to agent's agentworks profile
    shims_path = f"{home}/.local/share/mise/shims"
    try:
        profile_path = f"{home}/{AGENTWORKS_PROFILE}"
        _run_as_agent(
            target, linux_user,
            f"printf '%s' 'export PATH=\"{shims_path}:$PATH\"\n' >> {profile_path}",
        )
        source_line = f". {profile_path}"
        for rc in [f"{home}/.profile", f"{home}/.zprofile"]:
            _run_as_agent(
                target, linux_user,
                f"grep -q {AGENTWORKS_PROFILE} {rc} 2>/dev/null"
                f" || printf '%s\\n' '{source_line}' >> {rc}",
            )
    except SSHError as e:
        typer.echo(f"  Warning: agent profile configuration failed: {e}", err=True)

    # Write mise activation to agent's rc (interactive shell hooks)
    if agent_cfg.mise_activate:
        try:
            rc_path = f"{home}/{AGENTWORKS_RC}"
            rc_content = f"# Managed by agentworks -- do not edit\n{MISE_ACTIVATE_LINES}\n"
            _write_agent_file(target, linux_user, rc_path, rc_content)
            source_line = f". {rc_path}"
            for rc in [f"{home}/.bashrc", f"{home}/.zshrc"]:
                _run_as_agent(
                    target, linux_user,
                    f"grep -q {AGENTWORKS_RC} {rc} 2>/dev/null"
                    f" || printf '%s\\n' '{source_line}' >> {rc}",
                )
        except SSHError as e:
            typer.echo(f"  Warning: agent rc configuration failed: {e}", err=True)

    mise_config_dir = f"{home}/.config/mise"

    # Write mise config if packages declared
    if has_packages:
        typer.echo(f"  Writing mise config for agent ({len(agent_cfg.mise_packages)} packages)...")
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
            _run_as_agent(target, linux_user, f"mkdir -p {mise_config_dir}")
            _write_agent_file(target, linux_user, f"{mise_config_dir}/config.toml", mise_config)
        except SSHError as e:
            typer.echo(f"  Warning: agent mise config write failed: {e}", err=True)
            return

    # Copy lockfile if configured
    if has_lockfile and agent_cfg.mise_lockfile:
        typer.echo(f"  Fetching agent mise lockfile from {agent_cfg.mise_lockfile}...")
        try:
            from agentworks.sources import SourceRefError, fetch_file, parse_source_ref

            ref = parse_source_ref(agent_cfg.mise_lockfile, default_filename="mise.lock")
            from agentworks.ssh import ExecTarget

            exec_target = ExecTarget(ssh=ssh_target_for_vm(vm, config))
            _run_as_agent(target, linux_user, f"mkdir -p {mise_config_dir}")
            # Fetch to tmp (as admin, needs network), then move to agent home
            tmp_lock = f"/tmp/agentworks-{linux_user}-mise-lock"
            fetch_file(ref, exec_target, tmp_lock)
            run_as_root(target, f"mv {tmp_lock} {mise_config_dir}/mise.lock")
            run_as_root(target, f"chown {linux_user}:{linux_user} {mise_config_dir}/mise.lock")
        except (SourceRefError, SSHError) as e:
            typer.echo(f"  Warning: agent mise lockfile fetch failed: {e}", err=True)

    # Run mise install as the agent user
    lockfile_exists = False
    try:
        result = _run_as_agent(target, linux_user, f"test -f {mise_config_dir}/mise.lock", check=False)
        lockfile_exists = result.ok
    except SSHError:
        pass

    install_flags = "-y --locked" if lockfile_exists else "-y"
    try:
        _run_as_agent(target, linux_user, f"mise install {install_flags}", timeout=300)
        typer.echo("  Agent mise packages installed")
    except SSHError as e:
        if lockfile_exists and agent_cfg.mise_allow_unlocked:
            typer.echo("  Warning: some agent packages not in lockfile, installing unlocked...", err=True)
            try:
                _run_as_agent(target, linux_user, "mise install -y", timeout=300)
                typer.echo("  Agent mise packages installed (unlocked)")
            except SSHError as e2:
                typer.echo(f"  Warning: agent mise install failed: {e2}", err=True)
        else:
            typer.echo(f"  Warning: agent mise install failed: {e}", err=True)
            if lockfile_exists:
                typer.echo("  Hint: set mise_allow_unlocked = true to install unlocked packages", err=True)


def _build_agent_test_command(
    entry: UserInstallCommandEntry,
    linux_user: str,
    home: str,
) -> str | None:
    """Build a test command that runs as the agent user."""
    import shlex as _shlex

    test_exec: str | None = getattr(entry, "test_exec", None)
    test_file: str | None = getattr(entry, "test_file", None)
    test_dir: str | None = getattr(entry, "test_dir", None)
    if test_exec:
        # Run command -v as the agent user via su
        inner = f"command -v {_shlex.quote(test_exec)}"
        return f"su - {_shlex.quote(linux_user)} -c {_shlex.quote(inner)} > /dev/null 2>&1"
    if test_file:
        path = test_file.replace("~", home, 1) if test_file.startswith("~") else test_file
        return f"test -f {_shlex.quote(path)}"
    if test_dir:
        path = test_dir.replace("~", home, 1) if test_dir.startswith("~") else test_dir
        return f"test -d {_shlex.quote(path)}"
    return None


# -- Helpers ---------------------------------------------------------------


def _require_workspace(db: Database, workspace_name: str) -> WorkspaceRow:
    ws = db.get_workspace(workspace_name)
    if ws is None:
        typer.echo(f"Error: workspace '{workspace_name}' not found", err=True)
        raise typer.Exit(1)
    return ws


def _require_vm_for_workspace(db: Database, ws: WorkspaceRow) -> VMRow:
    assert ws.vm_name is not None
    vm = db.get_vm(ws.vm_name)
    if vm is None:
        typer.echo(f"Error: VM '{ws.vm_name}' not found", err=True)
        raise typer.Exit(1)
    return vm
