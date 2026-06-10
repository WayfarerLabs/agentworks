"""Agent lifecycle orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.config import validate_name
from agentworks.errors import (
    AlreadyExistsError,
    AuthorizationError,
    ExternalError,
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.ssh import admin_exec_target
from agentworks.vms.manager import keep_vm_active

if TYPE_CHECKING:
    from agentworks.catalog import UserInstallCommandEntry
    from agentworks.config import Config
    from agentworks.db import Database, VMRow, WorkspaceRow
    from agentworks.ssh import ExecTarget, SSHLogger, SSHResult

AGENT_PREFIX = "agt-"
WS_GROUP_PREFIX = "ws-"


def _run_as_agent(
    target: ExecTarget,
    linux_user: str,
    command: str,
    *,
    check: bool = True,
    timeout: int | None = None,
) -> SSHResult:
    """Run a command as an agent user via su -.

    Uses su - for a login shell so the agent's environment is set up.
    Logging is sourced from ``target.logger``; pass a logger-equipped
    target to capture output.
    """
    import shlex

    inner = shlex.quote(command)
    return target.run(
        f"su - {shlex.quote(linux_user)} -c {inner}",
        sudo=True,
        check=check,
        timeout=timeout,
    )


def _write_agent_file(
    target: ExecTarget,
    linux_user: str,
    dest: str,
    content: str,
    *,
    mode: str | None = None,
) -> None:
    """Write a file into an agent user's home via tmp + mv.

    scp runs as admin and can't write to the agent's home directly.
    Logging is sourced from ``target.logger``.
    """
    safe_name = linux_user.replace("/", "-")
    tmp_path = f"/tmp/agentworks-{safe_name}-{dest.rsplit('/', 1)[-1]}"
    target.write_file(tmp_path, content)
    target.run(f"mv {tmp_path} {dest}", sudo=True)
    target.run(f"chown {linux_user}:{linux_user} {dest}", sudo=True)
    if mode:
        target.run(f"chmod {mode} {dest}", sudo=True)


def derive_linux_user(agent_name: str) -> str:
    """Derive the Linux username for a newly-created agent: agt-<name>.

    Existing agents retain whatever username was stored in the database at
    their creation time (older agents use the legacy agt-- prefix). Always
    read agent_row.linux_user for the canonical value; this helper is only
    used at agent-create time.
    """
    return f"{AGENT_PREFIX}{agent_name}"


def workspace_group(workspace_name: str) -> str:
    """Derive the Linux group name for a newly-created workspace: ws-<name>.

    Existing workspaces retain whatever group was stored in the database at
    their creation time (legacy workspaces use the older ws-- prefix).
    Always read workspace_row.linux_group for the canonical value; this
    helper is only used at workspace-create time.
    """
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
        raise AlreadyExistsError(
            f"agent '{name}' already exists",
            entity_kind="agent",
            entity_name=name,
        )

    vm = _require_vm(db, vm_name)
    linux_user = derive_linux_user(name)

    # Collect credentials up front before any SSH work
    git_tokens = _collect_agent_credentials(config)

    from agentworks.ssh import SSHLogger
    ssh_logger = SSHLogger(vm.name, "agent-create")
    with keep_vm_active(db, config, vm):

        def _safe_rollback() -> None:
            # Best-effort: rollback failures must not mask the original KI or
            # exception. Surface them as a warning and let the original error
            # continue to propagate.
            try:
                _delete_agent_on_vm(vm, config, linux_user, logger=ssh_logger)
            except Exception as cleanup_err:
                output.warn(
                    f"rollback during agent create failed: {cleanup_err}. "
                    f"VM may have residual user/files for '{linux_user}'. "
                    f"SSH log: {ssh_logger.path}"
                )

        # The logger's close() writes a "Finished" footer; defer it via finally so
        # rollback commands are logged BEFORE the footer, not after.
        try:
            try:
                _create_agent_on_vm(vm, config, linux_user, git_tokens=git_tokens, logger=ssh_logger)
            except KeyboardInterrupt:
                output.warn(f"Cancelling agent create '{name}'... rolling back.")
                _safe_rollback()
                raise
            except Exception as e:
                _safe_rollback()
                raise ExternalError(
                    f"creating agent: {e}",
                    entity_kind="agent",
                    entity_name=name,
                    hint=f"SSH log: {ssh_logger.path}",
                ) from e
        finally:
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
            for ws in db.list_workspaces(vm_name=vm_name):
                _add_to_workspace_group(vm, config, db, linux_user, ws.name, logger=None)
                db.insert_agent_grant(name, ws.name, "explicit")

        # Refresh operator SSH config so `ssh <prefix><vm>--<agent>` works.
        # Declarative rebuild from DB state picks up the new agent row.
        from agentworks.ssh_config import sync_ssh_config

        sync_ssh_config(config, db)

        output.info(f"Agent '{name}' created on VM '{vm_name}' (user: {agent.linux_user})")


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
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    # Check for sessions using this agent
    all_sessions = db.list_sessions()
    agent_sessions = [s for s in all_sessions if s.agent_name == name]
    if agent_sessions and not force:
        for s in agent_sessions:
            output.detail(f"{s.name}")
        raise StateError(
            f"agent '{name}' has {len(agent_sessions)} session(s).",
            entity_kind="agent",
            entity_name=name,
            hint="Delete the sessions first, or pass --force to also stop them.",
        )

    if not yes:
        msg = f"Delete agent '{name}'?"
        if agent_sessions:
            msg += f" ({len(agent_sessions)} session(s) will also be stopped)"
        if not output.confirm(msg):
            raise UserAbort("delete cancelled")

    vm = _require_vm(db, agent.vm_name)

    from agentworks.ssh import SSHLogger
    ssh_logger = SSHLogger(vm.name, "agent-delete")
    with keep_vm_active(db, config, vm):

        # Kill running sessions for this agent (status-aware)
        if agent_sessions:
            from agentworks.db import SessionStatus
            from agentworks.sessions.manager import check_session_status, ensure_pids_batch
            from agentworks.sessions.tmux import force_kill_tmux_server, kill_session
            from agentworks.ssh import admin_exec_target

            target = admin_exec_target(vm, config, logger=ssh_logger)
            agent_sessions = ensure_pids_batch(agent_sessions, db=db, config=config)
            # Snapshot console memberships before db.delete_session cascades them.
            console_pairs = [
                (c.name, s.name)
                for s in agent_sessions
                for c in db.list_consoles_for_session(s.name)
            ]
            unstoppable: list[str] = []
            for session in agent_sessions:
                status = check_session_status(session, target=target)
                if status == SessionStatus.OK:
                    if not kill_session(session.name, run_command=target.run, socket_path=session.socket_path):
                        # Race: session may have exited between check and kill. Recheck.
                        recheck = check_session_status(session, target=target)
                        if recheck != SessionStatus.STOPPED:
                            unstoppable.append(session.name)
                            continue
                elif status == SessionStatus.BROKEN:
                    if session.pid and session.pid > 0 and force_kill_tmux_server(
                        session.pid, target=target, socket_path=session.socket_path,
                    ):
                        pass  # killed successfully
                    else:
                        unstoppable.append(session.name)
                elif status == SessionStatus.UNKNOWN:
                    unstoppable.append(session.name)
            if unstoppable:
                raise StateError(
                    f"cannot delete agent '{name}': {len(unstoppable)} session(s) could not be stopped "
                    f"({', '.join(unstoppable)}).",
                    entity_kind="agent",
                    entity_name=name,
                    hint="Resolve the stuck sessions manually before retrying.",
                )
            for session in agent_sessions:
                db.delete_session(session.name)
            output.detail(f"Deleted {len(agent_sessions)} session(s)")

            # Best-effort: take down dangling 'Waiting for session...' windows in
            # any console that listed one of these sessions.
            if console_pairs:
                from agentworks.sessions.multi_console import kill_session_windows

                kill_session_windows(target, pairs=console_pairs)

        # Remove from all workspace groups
        granted_workspaces = db.list_granted_workspaces(name)
        for ws_name in granted_workspaces:
            _remove_from_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=ssh_logger)

        _delete_agent_on_vm(vm, config, agent.linux_user, logger=ssh_logger)
        ssh_logger.close()

        db.delete_agent(name)

        # Refresh operator SSH config so the per-agent block disappears.
        from agentworks.ssh_config import sync_ssh_config

        sync_ssh_config(config, db)

        output.info(f"Agent '{name}' deleted")


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
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    agent_tmpl = resolve_template(config, agent.template)
    if agent.template and agent.template != "default":
        config = _replace(config, agent=agent_tmpl)

    vm = _require_vm(db, agent.vm_name)

    # Collect credentials up front before any SSH work
    git_tokens = _collect_agent_credentials(config)

    from agentworks.ssh import SSHLogger
    ssh_logger = SSHLogger(vm.name, "agent-reinit")
    with keep_vm_active(db, config, vm):
        try:
            try:
                _create_agent_on_vm(vm, config, agent.linux_user, git_tokens=git_tokens, logger=ssh_logger)
            except KeyboardInterrupt:
                output.warn(
                    f"Cancelling agent reinit '{name}'. The agent may be in a partial state. "
                    f"Re-run 'agent reinit {name}' to retry. SSH log: {ssh_logger.path}"
                )
                raise
            except Exception as e:
                raise ExternalError(
                    f"reinitializing agent: {e}",
                    entity_kind="agent",
                    entity_name=name,
                    hint=f"SSH log: {ssh_logger.path}",
                ) from e
        finally:
            ssh_logger.close()

        # Refresh operator SSH config (declarative rebuild; picks up any
        # config changes that affect the per-agent block).
        from agentworks.ssh_config import sync_ssh_config

        sync_ssh_config(config, db)

        output.info(f"Agent '{name}' reinitialized")


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
            _remove_from_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=ssh_logger)
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
    vm_name: str | list[str] | None = None,
) -> None:
    """List agents."""
    agents = db.list_agents(vm_name=vm_name)
    if not agents:
        output.info("No agents found.")
        return

    output.info(f"{'NAME':<20} {'VM':<15} {'TEMPLATE':<12} {'WORKSPACE GRANTS'}")
    output.info("-" * 80)
    for agent in agents:
        grants = _format_grants(db, agent.name, agent.grant_all)
        output.info(f"{agent.name:<20} {agent.vm_name:<15} {agent.template or '-':<12} {grants}")


def describe_agent(
    db: Database,
    *,
    name: str,
) -> None:
    """Show detailed information about an agent."""
    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    output.info(f"Name:       {agent.name}")
    output.info(f"VM:         {agent.vm_name}")
    output.info(f"Linux user: {agent.linux_user}")
    output.info(f"Template:   {agent.template or '-'}")
    output.info(f"Grant all:  {'yes' if agent.grant_all else 'no'}")
    output.info(f"Created:    {agent.created_at}")

    # Explicit grants
    grants = db.list_granted_workspaces_with_types(name)
    explicit = [ws for ws, has_explicit, _ in grants if has_explicit]
    output.info(f"\nExplicit grants ({len(explicit)}):")
    if explicit:
        for ws in explicit:
            output.detail(ws)
    else:
        output.detail("(none)")

    # Sessions (which also show implicit grants)
    all_sessions = db.list_sessions()
    agent_sessions = [s for s in all_sessions if s.agent_name == name]
    output.info(f"\nSessions ({len(agent_sessions)}):")
    if agent_sessions:
        for s in agent_sessions:
            output.detail(f"{s.name}  [{s.template}]  workspace: {s.workspace_name}")
    else:
        output.detail("(none)")


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
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    vm = _require_vm(db, agent.vm_name)

    from agentworks.workspaces.manager import _ensure_vm_running

    _ensure_vm_running(db, config, vm)

    import sys

    from agentworks.ssh import interactive

    target = admin_exec_target(vm, config)

    with keep_vm_active(db, config, vm):
        if workspace_name:
            ws = db.get_workspace(workspace_name)
            if ws is None:
                raise NotFoundError(
                    f"workspace '{workspace_name}' not found",
                    entity_kind="workspace",
                    entity_name=workspace_name,
                )
            if not db.has_any_grant(name, workspace_name):
                raise AuthorizationError(
                    f"agent '{name}' does not have access to workspace '{workspace_name}'",
                    entity_kind="agent",
                    entity_name=name,
                    hint=f"Run 'agent grant-workspaces {name} {workspace_name}' to grant access.",
                )
            import shlex

            q_path = shlex.quote(ws.workspace_path)
            shell_cmd = f"exec sudo su --login {agent.linux_user} -c 'cd {q_path} && exec $SHELL -li'"
            sys.exit(interactive(target, shell_cmd))
        else:
            sys.exit(interactive(target, f"exec sudo su --login {agent.linux_user}"))


def exec_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    command: list[str],
) -> int:
    """Execute a command as an agent user on a VM via direct SSH subprocess.

    Returns the remote exit code.
    """
    import shlex
    import subprocess

    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    vm = _require_vm(db, agent.vm_name)
    if vm.tailscale_host is None:
        raise StateError(
            f"VM '{vm.name}' has no Tailscale IP",
            entity_kind="vm",
            entity_name=vm.name,
            hint="VM init may not be complete. Check 'vm describe' for status.",
        )

    remote_cmd = command[0] if len(command) == 1 else shlex.join(command)
    su_cmd = f"sudo -n su --login {agent.linux_user} -c {shlex.quote(remote_cmd)}"

    ssh_cmd = ["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    if config.operator.ssh_private_key:
        ssh_cmd.extend(["-i", str(config.operator.ssh_private_key)])
    ssh_cmd.append(f"{vm.admin_username}@{vm.tailscale_host}")
    ssh_cmd.append(su_cmd)

    with keep_vm_active(db, config, vm):
        return subprocess.call(ssh_cmd)


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
    if not grant_all and not workspace_names:
        raise ValidationError(
            f"grant for '{agent_name}' needs at least one workspace name "
            f"or workspace_names empty + grant_all=True",
            entity_kind="agent",
            entity_name=agent_name,
        )

    agent = db.get_agent(agent_name)
    if agent is None:
        raise NotFoundError(
            f"agent '{agent_name}' not found",
            entity_kind="agent",
            entity_name=agent_name,
        )

    vm = _require_vm(db, agent.vm_name)
    with keep_vm_active(db, config, vm):

        if grant_all:
            db.update_agent_grant_all(agent_name, True)
            # Add to all existing workspace groups on this VM
            for ws in db.list_workspaces(vm_name=vm.name):
                _add_to_workspace_group(vm, config, db, agent.linux_user, ws.name, logger=None)
                db.insert_agent_grant(agent_name, ws.name, "explicit")
            output.info(f"Agent '{agent_name}' granted access to all workspaces")
            return

        for ws_name in workspace_names:
            found_ws = db.get_workspace(ws_name)
            if found_ws is None:
                output.warn(f"workspace '{ws_name}' not found, skipping")
                continue
            _add_to_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=None)
            db.insert_agent_grant(agent_name, ws_name, "explicit")
            output.detail(f"Granted: {ws_name}")


def revoke_workspaces(
    db: Database,
    config: Config,
    *,
    agent_name: str,
    workspace_names: list[str],
    revoke_all: bool = False,
) -> None:
    """Revoke explicit workspace grants from an agent."""
    if not revoke_all and not workspace_names:
        raise ValidationError(
            f"revoke for '{agent_name}' needs at least one workspace name "
            f"or workspace_names empty + revoke_all=True",
            entity_kind="agent",
            entity_name=agent_name,
        )

    agent = db.get_agent(agent_name)
    if agent is None:
        raise NotFoundError(
            f"agent '{agent_name}' not found",
            entity_kind="agent",
            entity_name=agent_name,
        )

    vm = _require_vm(db, agent.vm_name)
    with keep_vm_active(db, config, vm):

        if revoke_all:
            db.update_agent_grant_all(agent_name, False)
            db.delete_explicit_grants(agent_name)
            # Remove from groups where no implicit grants remain
            remaining_implicit: list[str] = []
            granted = db.list_granted_workspaces(agent_name)
            for ws_name in granted:
                if db.has_any_grant(agent_name, ws_name):
                    remaining_implicit.append(ws_name)
                else:
                    _remove_from_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=None)
            output.info(f"All explicit grants revoked for agent '{agent_name}'")
            if remaining_implicit:
                output.warn(
                    f"agent still has implicit access via sessions to: {', '.join(remaining_implicit)}"
                )
            return

        for ws_name in workspace_names:
            db.delete_agent_grant(agent_name, ws_name, "explicit")
            if not db.has_any_grant(agent_name, ws_name):
                _remove_from_workspace_group(vm, config, db, agent.linux_user, ws_name, logger=None)
                output.detail(f"Revoked: {ws_name}")
            else:
                output.detail(f"Revoked: {ws_name} (still has implicit access via sessions)")


# -- VM operations ---------------------------------------------------------


def _resolve_ws_group(db: Database, workspace_name: str) -> str:
    """Look up the Linux group stored for a workspace.

    Callers must use the recorded group rather than re-deriving it, so
    legacy workspaces (with the older ws-- prefix on their on-VM group)
    keep working after the prefix change.
    """
    ws = db.get_workspace(workspace_name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{workspace_name}' not found",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    return ws.linux_group


def _add_to_workspace_group(
    vm: VMRow,
    config: Config,
    db: Database,
    linux_user: str,
    workspace_name: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Add an agent user to a workspace's Linux group."""
    target = admin_exec_target(vm, config, logger=logger)
    ws_grp = _resolve_ws_group(db, workspace_name)
    # Ensure group exists (idempotent)
    target.run(f"sh -c 'getent group {ws_grp} >/dev/null 2>&1 || /usr/sbin/groupadd {ws_grp}'", sudo=True)
    target.run(f"usermod -aG {ws_grp} {linux_user}", sudo=True)


def _remove_from_workspace_group(
    vm: VMRow,
    config: Config,
    db: Database,
    linux_user: str,
    workspace_name: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Remove an agent user from a workspace's Linux group."""
    target = admin_exec_target(vm, config, logger=logger)
    ws_grp = _resolve_ws_group(db, workspace_name)
    target.run(f"gpasswd -d {linux_user} {ws_grp}", sudo=True, check=False)


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
    target = admin_exec_target(vm, config, logger=logger)

    output.detail(f"Creating user '{linux_user}' on VM '{vm.name}'...")
    home = f"/home/{linux_user}"

    agent_cfg = config.agent
    agent_shell = agent_cfg.shell

    # Create user with the template's shell (idempotent: skip if exists)
    shell_path = f"/bin/{agent_shell}" if "/" not in agent_shell else agent_shell
    user_exists = target.run(f"id {linux_user}", sudo=True, check=False)
    if not user_exists.ok:
        target.run(f"useradd -m -s {shell_path} {linux_user}", sudo=True)
    else:
        target.run(f"usermod -s {shell_path} {linux_user}", sudo=True)

    # Reconcile the agent's authorized_keys so direct SSH as the agent works
    # (FRD R3). Identical call shape at create and reinit (declarative
    # parity); the helper handles the agent's ~/.ssh creation on first run.
    if logger is not None:
        from agentworks.vms.initializer import _reconcile_authorized_keys

        _reconcile_authorized_keys(
            target,
            config,
            home=home,
            logger=logger,
            owner=linux_user,
        )

    # Ensure the agent tmux socket infrastructure exists. Call
    # ensure_agent_socket_root first so this works on VMs that haven't
    # been reinited since the socket feature was added.
    from agentworks.sessions.tmux import cleanup_stale_sockets, ensure_agent_socket_dir, ensure_agent_socket_root

    ensure_agent_socket_root(target, vm.admin_username)
    # The per-agent dir won't exist for a brand-new agent -- suppress the
    # "missing" warning. Misconfiguration of an existing dir still warns.
    ensure_agent_socket_dir(target, linux_user, warn_if_missing=False)
    removed = cleanup_stale_sockets(target, linux_user)
    if removed:
        output.detail(f"Cleaned up {removed} stale socket(s)")

    # Write a minimal rc file with a clear agent prompt
    if agent_shell == "zsh":
        rc_content = f"export PS1='[agent:{linux_user}] %~%# '\n"
        rc_file = f"{home}/.zshrc"
    elif agent_shell == "bash":
        rc_content = f"export PS1='[agent:{linux_user}] \\w\\$ '\n"
        rc_file = f"{home}/.bashrc"
    else:
        output.warn(f"unsupported shell '{agent_shell}', skipping prompt configuration")
        rc_content = None
        rc_file = None

    if rc_content and rc_file:
        _write_agent_file(target, linux_user, rc_file, rc_content)

    # Git safe.directory wildcard (agents access repos owned by admin)
    if config.admin.git_force_safe_directory:
        try:
            _run_as_agent(target, linux_user, "git config --global --add safe.directory '*'")
            output.detail("Git safe.directory configured for agent")
        except Exception as e:
            output.warn(f"agent git safe.directory setup failed: {e}")

    # Git credentials for the agent (tokens collected up front)
    if agent_cfg.git_credentials and git_tokens:
        from agentworks.vms.initializer import resolve_git_credential_providers

        output.detail("Configuring git credentials for agent...")
        try:
            providers = resolve_git_credential_providers(config, agent_cfg.git_credentials)
            cred_lines: list[str] = []
            for cred_name, provider in providers.items():
                token = git_tokens.get(cred_name)
                if token:
                    cred_lines.extend(provider.credential_lines(token))
            if cred_lines:
                cred_content = "\n".join(cred_lines) + "\n"
                _write_agent_file(target, linux_user, f"{home}/.git-credentials", cred_content, mode="600")
                _run_as_agent(target, linux_user, "git config --global credential.helper store")
        except Exception as e:
            output.warn(f"agent git credential setup failed: {e}")

    # User install commands for the agent
    _run_agent_install_commands(vm, config, linux_user, home, logger=logger)

    # Dotfiles for the agent
    if agent_cfg.dotfiles_source:
        output.detail(f"Syncing agent dotfiles from {agent_cfg.dotfiles_source}...")
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
                    check=False,
                )
                if is_git.ok:
                    remote = _run_as_agent(
                        target, linux_user,
                        f"git -C {_shlex.quote(dest)} remote get-url origin",
                        check=False,
                    )
                    if remote.ok and remote.stdout.strip() == ref.path:
                        output.detail("Dotfiles already cloned, pulling latest...")
                        if ref.ref:
                            _run_as_agent(
                                target, linux_user,
                                f"git -C {_shlex.quote(dest)} fetch",
                                check=False, timeout=120,
                            )
                            checkout = _run_as_agent(
                                target, linux_user,
                                f"git -C {_shlex.quote(dest)} checkout {_shlex.quote(ref.ref)}",
                                check=False,
                            )
                            if not checkout.ok:
                                output.warn(
                                    f"dotfiles checkout of '{ref.ref}' failed, skipping"
                                )
                        else:
                            pull = _run_as_agent(
                                target, linux_user,
                                f"git -C {_shlex.quote(dest)} pull",
                                check=False, timeout=120,
                            )
                            if not pull.ok:
                                output.warn(
                                    "dotfiles pull failed (local changes?), skipping"
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
                    _run_as_agent(target, linux_user, clone_cmd, timeout=120)
            else:
                # Local source: copy as admin then chown
                tmp_dotfiles = f"/tmp/agentworks-{linux_user}-dotfiles"
                target.run(f"rm -rf {tmp_dotfiles}", check=False)
                from agentworks.sources import fetch_dir

                fetch_dir(ref, target, tmp_dotfiles)
                target.run(f"mv {tmp_dotfiles} {dest}", sudo=True)
                target.run(f"chown -R {linux_user}:{linux_user} {dest}", sudo=True)

            output.detail(f"Running agent dotfiles install: {agent_cfg.dotfiles_install_cmd}")
            _run_as_agent(
                target,
                linux_user,
                f"cd {dest} && {agent_cfg.dotfiles_install_cmd}",
                timeout=120,
            )
        except (SourceRefError, Exception) as e:
            output.warn(f"agent dotfiles failed: {e}")

    # Mise for the agent
    _run_agent_mise_setup(vm, config, linux_user, home, logger=logger)

    # Install nerf Claude plugin for the agent
    if config.agent.nerf_install_claude_plugin:
        _install_nerf_claude_plugin_for_agent(target, linux_user, agent_shell)

    # Claude Code marketplaces and plugins for the agent
    from agentworks.vms.initializer import install_claude_plugins

    install_claude_plugins(
        lambda cmd, timeout: _run_as_agent(target, linux_user, cmd, timeout=timeout),
        config.agent.claude_marketplaces,
        config.agent.claude_plugins,
    )


def _install_nerf_claude_plugin_for_agent(
    target: ExecTarget,
    linux_user: str,
    shell: str,
) -> None:
    """Install the nerf Claude Code plugin for an agent user. Non-fatal."""
    from agentworks.ssh import SSHError

    try:
        check = _run_as_agent(
            target,
            linux_user,
            f"{shell} -lc 'test -x $AGENTWORKS_NERF_HOME/claude-plugin/scripts/install-plugin'",
            check=False,
        )
        if not check.ok:
            output.warn(
                "nerf Claude plugin not found on this VM. "
                "Set nerf_build_claude_plugin = true in your VM template and reinit."
            )
            return

        output.detail("Installing nerf Claude plugin for agent...")
        _run_as_agent(
            target,
            linux_user,
            f"{shell} -lc '$AGENTWORKS_NERF_HOME/claude-plugin/scripts/install-plugin'",
            timeout=30,
        )
        output.detail("Nerf Claude plugin installed for agent")
    except SSHError as e:
        output.warn(f"agent nerf plugin install failed: {e}")


def _delete_agent_on_vm(
    vm: VMRow,
    config: Config,
    linux_user: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Remove an agent Linux user from a VM."""
    from agentworks.ssh import SSHError

    target = admin_exec_target(vm, config, logger=logger)

    try:
        # Kill any running processes for the user
        target.run(f"pkill -u {linux_user}", sudo=True, check=False)
        # Remove the user and their home directory
        target.run(f"userdel -r {linux_user}", sudo=True)
    except SSHError as e:
        output.warn(f"remote cleanup for '{linux_user}' failed: {e}")


def _run_agent_install_commands(
    vm: VMRow,
    config: Config,
    linux_user: str,
    home: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Run user install commands for an agent. Failures warn but do not abort."""
    command_names = config.agent.user_install_commands
    if not command_names:
        return

    import shlex

    from agentworks.catalog import load_catalog
    from agentworks.ssh import SSHError

    catalog = load_catalog(config)
    target = admin_exec_target(vm, config, logger=logger)
    shell = config.agent.shell
    path_additions: list[str] = []
    total = len(command_names)

    for i, name in enumerate(command_names, 1):
        entry = catalog.user_install_commands.get(name)
        if entry is None:
            output.warn(f"install command '{name}' not found in catalog")
            continue
        # Skip if already installed for this user (short timeout)
        test_cmd = _build_agent_test_command(entry, linux_user, home)
        if test_cmd:
            try:
                check = target.run(test_cmd, sudo=True, check=False, timeout=10)
                if check.returncode == 0:
                    output.detail(f"Agent install command {i}/{total} ({name}): already installed, skipping")
                    path_additions.extend(entry.path)
                    continue
            except SSHError as e:
                output.warn(f"install check for '{name}' failed ({e}), assuming not installed")

        truncated = entry.command[:60]
        output.detail(f"Agent install command {i}/{total} ({name}): {truncated}...")
        try:
            # Run as the agent user via su, in their login shell
            target.run(
                f"su - {shlex.quote(linux_user)} -c {shlex.quote(f'{shell} -lc {shlex.quote(entry.command)}')}",
                sudo=True,
                timeout=120,
            )
        except SSHError as e:
            output.warn(f"agent install command '{name}' failed: {e}")
        path_additions.extend(entry.path)

    # Write PATH additions for the agent
    if path_additions:
        from agentworks.vms.initializer import AGENTWORKS_PROFILE

        output.detail(f"Adding {len(path_additions)} PATH entries for agent...")
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
            output.warn(f"agent PATH configuration failed: {e}")


def _run_agent_mise_setup(
    vm: VMRow,
    config: Config,
    linux_user: str,
    home: str,
    *,
    logger: SSHLogger | None = None,
) -> None:
    """Set up mise for an agent: shims PATH, config, lockfile, install."""
    from agentworks.ssh import SSHError

    target = admin_exec_target(vm, config, logger=logger)
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
        output.warn(f"agent profile configuration failed: {e}")

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
            output.warn(f"agent rc configuration failed: {e}")

    mise_config_dir = f"{home}/.config/mise"

    # Write mise config if packages declared
    if has_packages:
        output.detail(f"Writing mise config for agent ({len(agent_cfg.mise_packages)} packages)...")
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
            output.warn(f"agent mise config write failed: {e}")
            return

    # Copy lockfile if configured
    if has_lockfile and agent_cfg.mise_lockfile:
        output.detail(f"Fetching agent mise lockfile from {agent_cfg.mise_lockfile}...")
        try:
            from agentworks.sources import SourceRefError, fetch_file, parse_source_ref

            ref = parse_source_ref(agent_cfg.mise_lockfile, default_filename="mise.lock")
            _run_as_agent(target, linux_user, f"mkdir -p {mise_config_dir}")
            # Fetch to tmp (as admin, needs network), then move to agent home
            tmp_lock = f"/tmp/agentworks-{linux_user}-mise-lock"
            fetch_file(ref, target, tmp_lock)
            target.run(f"mv {tmp_lock} {mise_config_dir}/mise.lock", sudo=True)
            target.run(f"chown {linux_user}:{linux_user} {mise_config_dir}/mise.lock", sudo=True)
        except (SourceRefError, SSHError) as e:
            output.warn(f"agent mise lockfile fetch failed: {e}")

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
        output.detail("Agent mise packages installed")
        installed = True
    except SSHError as e:
        if lockfile_exists and agent_cfg.mise_allow_unlocked:
            output.warn("some agent packages not in lockfile, installing unlocked...")
            try:
                _run_as_agent(target, linux_user, "mise install -y", timeout=300)
                output.detail("Agent mise packages installed (unlocked)")
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
        raise NotFoundError(
            f"VM '{vm_name}' not found",
            entity_kind="vm",
            entity_name=vm_name,
        )
    return vm


def _require_workspace(db: Database, workspace_name: str) -> WorkspaceRow:
    ws = db.get_workspace(workspace_name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{workspace_name}' not found",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    return ws


def _require_vm_for_workspace(db: Database, ws: WorkspaceRow) -> VMRow:
    vm = db.get_vm(ws.vm_name)
    if vm is None:
        raise NotFoundError(
            f"VM '{ws.vm_name}' not found",
            entity_kind="vm",
            entity_name=ws.vm_name,
        )
    return vm
