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
    from agentworks.ssh import SSHLogger, SSHResult, SSHTarget

AGENT_PREFIX = "agt--"
WS_GROUP_PREFIX = "ws--"


def _run_as_agent(
    target: SSHTarget,
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
    target: SSHTarget,
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


def derive_linux_user(agent_name: str) -> str:
    """Derive the Linux username for an agent: agt--<name>."""
    return f"{AGENT_PREFIX}{agent_name}"


def workspace_group(workspace_name: str) -> str:
    """Derive the Linux group name for a workspace: ws--<name>."""
    return f"{WS_GROUP_PREFIX}{workspace_name}"


def create_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    vm_name: str,
    template: str | None = None,
    grant_all_workspaces: bool = False,
) -> None:
    """Create an agent on a VM."""
    from dataclasses import replace as _replace

    from agentworks.agents.templates import resolve_template

    agent_tmpl = resolve_template(config, template)

    if template is not None:
        config = _replace(config, agent=agent_tmpl)

    validate_name(name)

    if db.get_agent(name) is not None:
        typer.echo(f"Error: agent '{name}' already exists", err=True)
        raise typer.Exit(1)

    vm = _require_vm(db, vm_name)
    linux_user = derive_linux_user(name)

    # Collect credentials up front before any SSH work
    git_tokens = _collect_agent_credentials(config)

    from agentworks.ssh import SSHLogger

    ssh_logger = SSHLogger(vm.name, "agent-create")
    try:
        _create_agent_on_vm(vm, config, linux_user, git_tokens=git_tokens, logger=ssh_logger)
    except Exception as e:
        ssh_logger.close()
        typer.echo(f"Error creating agent: {e}", err=True)
        typer.echo(f"  SSH log: {ssh_logger.path}", err=True)
        typer.echo(f"  Cleaning up user '{linux_user}'...", err=True)
        _delete_agent_on_vm(vm, config, linux_user, logger=ssh_logger)
        raise typer.Exit(1) from None
    ssh_logger.close()

    agent = db.insert_agent(
        name,
        vm_name,
        linux_user,
        template=agent_tmpl.name,
        grant_all=grant_all_workspaces,
    )

    # If grant_all, add to all existing workspace groups
    if grant_all_workspaces:
        workspaces = db.list_workspaces(vm_name=vm_name)
        for ws in workspaces:
            if ws.type == "vm":
                _add_to_workspace_group(vm, config, linux_user, ws.name, logger=None)
                db.insert_agent_grant(name, ws.name, "explicit")

    typer.echo(f"Agent '{name}' created on VM '{vm_name}' (user: {agent.linux_user})")


def delete_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    force: bool = False,
    yes: bool = False,
) -> None:
    """Delete an agent from a VM."""
    agent = db.get_agent(name)
    if agent is None:
        typer.echo(f"Error: agent '{name}' not found", err=True)
        raise typer.Exit(1)

    # Check for sessions using this agent
    all_sessions = db.list_sessions()
    agent_sessions = [s for s in all_sessions if s.agent_name == name]
    if agent_sessions and not force:
        typer.echo(
            f"Error: agent '{name}' has {len(agent_sessions)} session(s). Delete them first, or use --force.",
            err=True,
        )
        for s in agent_sessions:
            typer.echo(f"  {s.name}  [{s.status}]", err=True)
        raise typer.Exit(1)

    if not yes:
        msg = f"Delete agent '{name}'?"
        if agent_sessions:
            msg += f" ({len(agent_sessions)} session(s) will also be stopped)"
        typer.confirm(msg, abort=True)

    vm = _require_vm(db, agent.vm_name)

    from agentworks.ssh import SSHLogger

    ssh_logger = SSHLogger(vm.name, "agent-delete")

    # Kill running sessions for this agent
    if agent_sessions:
        from functools import partial

        from agentworks.ssh import run, ssh_target_for_vm
        from agentworks.sessions.tmux import kill_session

        target = ssh_target_for_vm(vm, config)
        run_command = partial(run, target, logger=ssh_logger)
        for session in agent_sessions:
            sock = session.socket_path
            if sock:
                kill_session(session.name, run_command=run_command, socket_path=sock)
            kill_session(session.name, run_command=run_command)
            db.delete_session(session.name)
        typer.echo(f"  Deleted {len(agent_sessions)} session(s)")

    # Remove from all workspace groups
    granted_workspaces = db.list_granted_workspaces(name)
    for ws_name in granted_workspaces:
        _remove_from_workspace_group(vm, config, agent.linux_user, ws_name, logger=ssh_logger)

    _delete_agent_on_vm(vm, config, agent.linux_user, logger=ssh_logger)
    ssh_logger.close()

    db.delete_agent(name)

    typer.echo(f"Agent '{name}' deleted")


def reinit_agent(
    db: Database,
    config: Config,
    *,
    name: str,
) -> None:
    """Re-run agent setup using the stored template."""
    from dataclasses import replace as _replace

    from agentworks.agents.templates import resolve_template

    agent = db.get_agent(name)
    if agent is None:
        typer.echo(f"Error: agent '{name}' not found", err=True)
        raise typer.Exit(1)

    agent_tmpl = resolve_template(config, agent.template)
    if agent.template and agent.template != "default":
        config = _replace(config, agent=agent_tmpl)

    vm = _require_vm(db, agent.vm_name)

    # Collect credentials up front before any SSH work
    git_tokens = _collect_agent_credentials(config)

    from agentworks.ssh import SSHLogger

    ssh_logger = SSHLogger(vm.name, "agent-reinit")
    try:
        _create_agent_on_vm(vm, config, agent.linux_user, git_tokens=git_tokens, logger=ssh_logger)
    except Exception as e:
        ssh_logger.close()
        typer.echo(f"Error reinitializing agent: {e}", err=True)
        typer.echo(f"  SSH log: {ssh_logger.path}", err=True)
        raise typer.Exit(1) from None
    ssh_logger.close()

    typer.echo(f"Agent '{name}' reinitialized")


def revoke_workspace_grants(
    db: Database,
    config: Config,
    ws_name: str,
    vm: VMRow,
) -> None:
    """Remove all agent grants for a workspace (called during workspace deletion).

    Agents are VM-scoped and not deleted with workspaces. Only their grants
    and group memberships for this workspace are removed.
    """
    # Find agents that have grants for this workspace
    # We need to remove group membership for each
    from agentworks.ssh import SSHLogger

    ssh_logger = SSHLogger(vm.name, "workspace-delete-grants")
    agents = db.list_agents(vm_name=vm.name)
    for agent in agents:
        if db.has_any_grant(agent.name, ws_name):
            _remove_from_workspace_group(vm, config, agent.linux_user, ws_name, logger=ssh_logger)
    ssh_logger.close()


MAX_GRANTS_DISPLAY = 60


def _format_grants(db: Database, agent_name: str, grant_all: bool) -> str:
    """Format workspace grants for display in agent list."""
    if grant_all:
        return "--ALL--"

    grants = db.list_granted_workspaces_with_types(agent_name)
    if not grants:
        return "(none)"

    parts: list[str] = []
    for ws_name, has_explicit, has_implicit in grants:
        # Mark with * if implicit-only (no explicit grant)
        suffix = "*" if has_implicit and not has_explicit else ""
        parts.append(f"{ws_name}{suffix}")

    result = ", ".join(parts)
    if len(result) > MAX_GRANTS_DISPLAY:
        result = result[: MAX_GRANTS_DISPLAY - 3] + "..."
    return result


def list_agents(
    db: Database,
    *,
    vm_name: str | None = None,
) -> None:
    """List agents."""
    agents = db.list_agents(vm_name=vm_name)
    if not agents:
        typer.echo("No agents found.")
        return

    typer.echo(f"{'NAME':<20} {'VM':<15} {'TEMPLATE':<12} {'WORKSPACE GRANTS'}")
    typer.echo("-" * 80)
    for agent in agents:
        grants = _format_grants(db, agent.name, agent.grant_all)
        typer.echo(f"{agent.name:<20} {agent.vm_name:<15} {agent.template or '-':<12} {grants}")


def describe_agent(
    db: Database,
    *,
    name: str,
) -> None:
    """Show detailed information about an agent."""
    agent = db.get_agent(name)
    if agent is None:
        typer.echo(f"Error: agent '{name}' not found", err=True)
        raise typer.Exit(1)

    typer.echo(f"Name:       {agent.name}")
    typer.echo(f"VM:         {agent.vm_name}")
    typer.echo(f"Linux user: {agent.linux_user}")
    typer.echo(f"Template:   {agent.template or '-'}")
    typer.echo(f"Grant all:  {'yes' if agent.grant_all else 'no'}")
    typer.echo(f"Created:    {agent.created_at}")

    # Explicit grants
    grants = db.list_granted_workspaces_with_types(name)
    explicit = [ws for ws, has_explicit, _ in grants if has_explicit]
    typer.echo(f"\nExplicit grants ({len(explicit)}):")
    if explicit:
        for ws in explicit:
            typer.echo(f"  {ws}")
    else:
        typer.echo("  (none)")

    # Sessions (which also show implicit grants)
    all_sessions = db.list_sessions()
    agent_sessions = [s for s in all_sessions if s.agent_name == name]
    typer.echo(f"\nSessions ({len(agent_sessions)}):")
    if agent_sessions:
        for s in agent_sessions:
            typer.echo(f"  {s.name}  [{s.template}]  {s.status}  workspace: {s.workspace_name}")
    else:
        typer.echo("  (none)")


def shell_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str | None = None,
) -> None:
    """Open a shell as an agent user on a VM."""
    agent = db.get_agent(name)
    if agent is None:
        typer.echo(f"Error: agent '{name}' not found", err=True)
        raise typer.Exit(1)

    vm = _require_vm(db, agent.vm_name)

    from agentworks.workspaces.manager import _ensure_vm_running

    _ensure_vm_running(db, config, vm)

    import sys

    from agentworks.ssh import interactive

    target = ssh_target_for_vm(vm, config)

    if workspace_name:
        ws = db.get_workspace(workspace_name)
        if ws is None:
            typer.echo(f"Error: workspace '{workspace_name}' not found", err=True)
            raise typer.Exit(1)
        if not db.has_any_grant(name, workspace_name):
            typer.echo(f"Error: agent '{name}' does not have access to workspace '{workspace_name}'", err=True)
            raise typer.Exit(1)
        import shlex

        q_path = shlex.quote(ws.workspace_path)
        sys.exit(interactive(target, f"exec sudo su --login {agent.linux_user} -c 'cd {q_path} && exec $SHELL -li'"))
    else:
        sys.exit(interactive(target, f"exec sudo su --login {agent.linux_user}"))


# -- VM operations ---------------------------------------------------------


def grant_workspaces(
    db: Database,
    config: Config,
    *,
    agent_name: str,
    workspace_names: list[str],
    grant_all: bool = False,
) -> None:
    """Grant an agent explicit access to workspaces."""
    agent = db.get_agent(agent_name)
    if agent is None:
        typer.echo(f"Error: agent '{agent_name}' not found", err=True)
        raise typer.Exit(1)

    vm = _require_vm(db, agent.vm_name)

    if grant_all:
        db.update_agent_grant_all(agent_name, True)
        # Add to all existing VM workspace groups
        workspaces = db.list_workspaces(vm_name=vm.name)
        for ws in workspaces:
            if ws.type == "vm":
                _add_to_workspace_group(vm, config, agent.linux_user, ws.name, logger=None)
                db.insert_agent_grant(agent_name, ws.name, "explicit")
        typer.echo(f"Agent '{agent_name}' granted access to all workspaces")
        return

    for ws_name in workspace_names:
        found_ws = db.get_workspace(ws_name)
        if found_ws is None:
            typer.echo(f"Warning: workspace '{ws_name}' not found, skipping", err=True)
            continue
        _add_to_workspace_group(vm, config, agent.linux_user, ws_name, logger=None)
        db.insert_agent_grant(agent_name, ws_name, "explicit")
        typer.echo(f"  Granted: {ws_name}")


def deny_workspaces(
    db: Database,
    config: Config,
    *,
    agent_name: str,
    workspace_names: list[str],
    deny_all: bool = False,
) -> None:
    """Remove explicit workspace grants from an agent."""
    agent = db.get_agent(agent_name)
    if agent is None:
        typer.echo(f"Error: agent '{agent_name}' not found", err=True)
        raise typer.Exit(1)

    vm = _require_vm(db, agent.vm_name)

    if deny_all:
        db.update_agent_grant_all(agent_name, False)
        db.delete_explicit_grants(agent_name)
        # Remove from groups where no implicit grants remain
        remaining_implicit: list[str] = []
        granted = db.list_granted_workspaces(agent_name)
        for ws_name in granted:
            if db.has_any_grant(agent_name, ws_name):
                remaining_implicit.append(ws_name)
            else:
                _remove_from_workspace_group(vm, config, agent.linux_user, ws_name, logger=None)
        typer.echo(f"All explicit grants removed for agent '{agent_name}'")
        if remaining_implicit:
            typer.echo(
                f"  Note: agent still has implicit access via sessions to: {', '.join(remaining_implicit)}",
                err=True,
            )
        return

    for ws_name in workspace_names:
        db.delete_agent_grant(agent_name, ws_name, "explicit")
        if not db.has_any_grant(agent_name, ws_name):
            _remove_from_workspace_group(vm, config, agent.linux_user, ws_name, logger=None)
            typer.echo(f"  Denied: {ws_name}")
        else:
            typer.echo(f"  Denied: {ws_name} (still has implicit access via sessions)")


def list_grants(
    db: Database,
    *,
    agent_name: str,
) -> None:
    """List workspace grants for an agent."""
    agent = db.get_agent(agent_name)
    if agent is None:
        typer.echo(f"Error: agent '{agent_name}' not found", err=True)
        raise typer.Exit(1)

    if agent.grant_all:
        typer.echo(f"Agent '{agent_name}' has grant-all enabled (access to all workspaces)")

    grants = db.list_granted_workspaces_with_types(agent_name)
    if not grants:
        typer.echo("No workspace grants.")
        return

    typer.echo(f"{'WORKSPACE':<25} {'TYPE'}")
    typer.echo("-" * 45)
    for ws_name, has_explicit, has_implicit in grants:
        if has_explicit and has_implicit:
            grant_type = "explicit + implicit"
        elif has_explicit:
            grant_type = "explicit"
        else:
            grant_type = "implicit (via sessions)"
        typer.echo(f"{ws_name:<25} {grant_type}")


# -- VM operations ---------------------------------------------------------


def _add_to_workspace_group(
    vm: VMRow,
    config: Config,
    linux_user: str,
    workspace_name: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Add an agent user to a workspace's Linux group."""
    from agentworks.ssh import run_as_root

    target = ssh_target_for_vm(vm, config)
    ws_grp = workspace_group(workspace_name)
    # Ensure group exists (idempotent)
    run_as_root(target, f"sh -c 'getent group {ws_grp} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_grp}'", logger=logger)
    run_as_root(target, f"usermod -aG {ws_grp} {linux_user}", logger=logger)


def _remove_from_workspace_group(
    vm: VMRow,
    config: Config,
    linux_user: str,
    workspace_name: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Remove an agent user from a workspace's Linux group."""
    from agentworks.ssh import run_as_root

    target = ssh_target_for_vm(vm, config)
    ws_grp = workspace_group(workspace_name)
    run_as_root(target, f"gpasswd -d {linux_user} {ws_grp}", check=False, logger=logger)


def _collect_agent_credentials(config: Config) -> dict[str, str]:
    """Collect git credentials up front before any SSH work begins."""
    agent_cfg = config.agent
    git_tokens: dict[str, str] = {}
    if agent_cfg.git_credentials:
        from agentworks.vms.initializer import resolve_git_credential_providers

        providers = resolve_git_credential_providers(config, agent_cfg.git_credentials)
        for cred_name, provider in providers.items():
            git_tokens[cred_name] = provider.obtain_token("agent")
    return git_tokens


def _create_agent_on_vm(
    vm: VMRow,
    config: Config,
    linux_user: str,
    *,
    git_tokens: dict[str, str] | None = None,
    logger: SSHLogger | None = None,
) -> None:
    """Create an agent Linux user on a VM and set up their environment.

    Workspace group membership is NOT set here - it is managed by the grant
    system. This function only creates the user and configures their tools.
    """
    from agentworks.ssh import run_as_root

    target = ssh_target_for_vm(vm, config)
    lg = logger

    typer.echo(f"  Creating user '{linux_user}' on VM '{vm.name}'...")
    home = f"/home/{linux_user}"

    agent_cfg = config.agent
    agent_shell = agent_cfg.shell

    # Create user with the template's shell (idempotent: skip if exists)
    shell_path = f"/bin/{agent_shell}" if "/" not in agent_shell else agent_shell
    user_exists = run_as_root(target, f"id {linux_user}", check=False, logger=lg)
    if not user_exists.ok:
        run_as_root(target, f"useradd -m -s {shell_path} {linux_user}", logger=lg)
    else:
        run_as_root(target, f"usermod -s {shell_path} {linux_user}", logger=lg)

    # Ensure the agent tmux socket infrastructure exists. Call
    # ensure_agent_socket_root first so this works on VMs that haven't
    # been reinited since the socket feature was added.
    from functools import partial as _partial

    from agentworks.sessions.tmux import ensure_agent_socket_dir, ensure_agent_socket_root

    _root_cmd = _partial(run_as_root, target, logger=lg)
    ensure_agent_socket_root(_root_cmd, vm.admin_username)
    ensure_agent_socket_dir(_root_cmd, linux_user)

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

    # Git safe.directory wildcard (agents access repos owned by admin)
    if config.admin.git_force_safe_directory:
        try:
            _run_as_agent(target, linux_user, "git config --global --add safe.directory '*'", logger=lg)
            typer.echo("  Git safe.directory configured for agent")
        except Exception as e:
            typer.echo(f"  Warning: agent git safe.directory setup failed: {e}", err=True)

    # Git credentials for the agent (tokens collected up front)
    if agent_cfg.git_credentials and git_tokens:
        from agentworks.vms.initializer import resolve_git_credential_providers

        typer.echo("  Configuring git credentials for agent...")
        try:
            providers = resolve_git_credential_providers(config, agent_cfg.git_credentials)
            cred_lines: list[str] = []
            for cred_name, provider in providers.items():
                token = git_tokens.get(cred_name)
                if token:
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

            # Clone/pull as the agent user (git credentials are already configured)
            if ref.kind == "git":
                import shlex as _shlex

                # If already cloned from the same repo, pull instead of clone
                is_git = _run_as_agent(
                    target, linux_user, f"test -d {_shlex.quote(dest)}/.git",
                    check=False, logger=lg,
                )
                if is_git.ok:
                    remote = _run_as_agent(
                        target, linux_user,
                        f"git -C {_shlex.quote(dest)} remote get-url origin",
                        check=False, logger=lg,
                    )
                    if remote.ok and remote.stdout.strip() == ref.path:
                        typer.echo("  Dotfiles already cloned, pulling latest...")
                        if ref.ref:
                            _run_as_agent(
                                target, linux_user,
                                f"git -C {_shlex.quote(dest)} fetch",
                                check=False, timeout=120, logger=lg,
                            )
                            checkout = _run_as_agent(
                                target, linux_user,
                                f"git -C {_shlex.quote(dest)} checkout {_shlex.quote(ref.ref)}",
                                check=False, logger=lg,
                            )
                            if not checkout.ok:
                                typer.echo(
                                    f"  Warning: dotfiles checkout of '{ref.ref}' failed, skipping",
                                    err=True,
                                )
                        else:
                            pull = _run_as_agent(
                                target, linux_user,
                                f"git -C {_shlex.quote(dest)} pull",
                                check=False, timeout=120, logger=lg,
                            )
                            if not pull.ok:
                                typer.echo(
                                    "  Warning: dotfiles pull failed (local changes?), skipping",
                                    err=True,
                                )
                    else:
                        raise SourceRefError(
                            f"dotfiles destination {dest} exists but is a different repo"
                        )
                else:
                    clone_cmd = f"git clone {_shlex.quote(ref.path)} {_shlex.quote(dest)}"
                    if ref.ref:
                        clone_cmd = (
                            f"git clone --branch {_shlex.quote(ref.ref)}"
                            f" {_shlex.quote(ref.path)} {_shlex.quote(dest)}"
                        )
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
                target,
                linux_user,
                f"cd {dest} && {agent_cfg.dotfiles_install_cmd}",
                timeout=120,
                logger=lg,
            )
        except (SourceRefError, Exception) as e:
            typer.echo(f"  Warning: agent dotfiles failed: {e}", err=True)

    # Mise for the agent
    _run_agent_mise_setup(vm, config, linux_user, home)

    # Install nerf Claude plugin for the agent
    if config.agent.nerf_install_claude_plugin:
        _install_nerf_claude_plugin_for_agent(target, linux_user, agent_shell)


def _install_nerf_claude_plugin_for_agent(
    target: SSHTarget,
    linux_user: str,
    shell: str,
) -> None:
    """Install the nerf Claude Code plugin for an agent user. Non-fatal."""
    from agentworks.ssh import SSHError

    try:
        check = _run_as_agent(
            target,
            linux_user,
            f"{shell} -lc 'test -x $AGENTWORKS_NERF_HOME/claude-plugin/scripts/nerfctl-install-plugin'",
            check=False,
        )
        if not check.ok:
            typer.echo(
                "  Warning: nerf Claude plugin not found on this VM. "
                "Set nerf_build_claude_plugin = true in your VM template and reinit.",
                err=True,
            )
            return

        typer.echo("  Installing nerf Claude plugin for agent...")
        _run_as_agent(
            target,
            linux_user,
            f"{shell} -lc '$AGENTWORKS_NERF_HOME/claude-plugin/scripts/nerfctl-install-plugin'",
            timeout=30,
        )
        typer.echo("  Nerf Claude plugin installed for agent")
    except SSHError as e:
        typer.echo(f"  Warning: agent nerf plugin install failed: {e}", err=True)


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
                    target,
                    linux_user,
                    f"grep -q {AGENTWORKS_PROFILE} {rc} 2>/dev/null || printf '%s\\n' '{source_line}' >> {rc}",
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
            target,
            linux_user,
            f"printf '%s' 'export PATH=\"{shims_path}:$PATH\"\n' >> {profile_path}",
        )
        source_line = f". {profile_path}"
        for rc in [f"{home}/.profile", f"{home}/.zprofile"]:
            _run_as_agent(
                target,
                linux_user,
                f"grep -q {AGENTWORKS_PROFILE} {rc} 2>/dev/null || printf '%s\\n' '{source_line}' >> {rc}",
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
                    target,
                    linux_user,
                    f"grep -q {AGENTWORKS_RC} {rc} 2>/dev/null || printf '%s\\n' '{source_line}' >> {rc}",
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

    installed = False
    install_flags = "-y --locked" if lockfile_exists else "-y"
    try:
        _run_as_agent(target, linux_user, f"mise install {install_flags}", timeout=300)
        typer.echo("  Agent mise packages installed")
        installed = True
    except SSHError as e:
        if lockfile_exists and agent_cfg.mise_allow_unlocked:
            typer.echo("  Warning: some agent packages not in lockfile, installing unlocked...", err=True)
            try:
                _run_as_agent(target, linux_user, "mise install -y", timeout=300)
                typer.echo("  Agent mise packages installed (unlocked)")
                installed = True
            except SSHError as e2:
                typer.echo(f"  Warning: agent mise install failed: {e2}", err=True)
        else:
            typer.echo(f"  Warning: agent mise install failed: {e}", err=True)
            if lockfile_exists:
                typer.echo("  Hint: set mise_allow_unlocked = true to install unlocked packages", err=True)

    # Prune stale tool versions not in the current config
    if installed and agent_cfg.mise_prune_on_reinit:
        import contextlib

        from agentworks.ssh import SSHError as _SSHError

        with contextlib.suppress(_SSHError):
            _run_as_agent(target, linux_user, "mise prune -y", timeout=60)


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


def _require_vm(db: Database, vm_name: str) -> VMRow:
    vm = db.get_vm(vm_name)
    if vm is None:
        typer.echo(f"Error: VM '{vm_name}' not found", err=True)
        raise typer.Exit(1)
    return vm


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
