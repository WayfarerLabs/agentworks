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
    from agentworks.db import AgentRow, Database, VMRow, WorkspaceRow
    from agentworks.ssh import ExecTarget, SSHLogger

AGENT_PREFIX = "agt-"
WS_GROUP_PREFIX = "ws-"


def _write_agent_file(
    target: ExecTarget,
    linux_user: str,
    dest: str,
    content: str,
    *,
    mode: str = "0644",
) -> None:
    """Write a file into an agent user's home via stage-and-install.

    Admin's SSH writes to a non-predictable mktemp path with 0600 perms,
    then ``install`` atomically places it at ``dest`` owned by the agent
    with the requested mode. This closes the cross-uid leakage window that
    a naive ``scp + mv + chown + chmod`` sequence opens, where the staging
    file is briefly world-readable to other agents on the VM.

    ``mode`` defaults to 0644 (matching the historical chmod-only behavior
    for callers that don't set it). Callers writing secrets pass an
    explicit restrictive mode (e.g. ``"0600"`` for ``.git-credentials``).
    """
    import shlex

    from agentworks.ssh import SSHError

    quoted_owner = shlex.quote(linux_user)
    quoted_dest = shlex.quote(dest)
    mktemp_result = target.run("mktemp --tmpdir agw-agentfile.XXXXXX")
    staging = (getattr(mktemp_result, "stdout", "") or "").strip()
    if not staging:
        raise SSHError("mktemp produced empty path")
    try:
        # Restrict the staging file to admin before content lands. Combined
        # with the mktemp randomized suffix, this prevents cross-uid reads
        # of secret material during the placement window.
        target.write_file(staging, content, mode="0600")
        target.run(
            f"install -o {quoted_owner} -g {quoted_owner} -m {shlex.quote(mode)} "
            f"{shlex.quote(staging)} {quoted_dest}",
            sudo=True,
        )
    finally:
        target.run(f"rm -f {shlex.quote(staging)}", check=False)


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


def _assert_agent_ssh_works(target: ExecTarget, agent: AgentRow) -> None:
    """Probe direct agent SSH; raise an actionable error on auth rejection.

    The direct-target-user-SSH rollout populates each agent's
    ``~/.ssh/authorized_keys`` with the operator's key set at agent create /
    reinit. Agents that existed before this rollout have a home directory
    with no ``.ssh/authorized_keys`` for the operator, so direct SSH as the
    agent is rejected. Catch that specific case here and turn the otherwise-
    opaque SSH transport failure into a clear "run ``agw agent reinit``"
    instruction.

    A probe round-trip costs ~50ms over Tailscale + ControlMaster. Cheaper
    than letting the failure surface mid-operation with partial state.

    Two failure shapes are distinguished:

    - Non-zero exit (SSH_TRANSPORT_ERROR = 255 typically): SSH connected
      and ``ssh`` itself reported an auth / transport failure. Treated as
      the pre-rollout case and raised as ``StateError`` with a reinit hint.
    - ``SSHError`` from ``target.run`` (timeout / unreachable host /
      ControlMaster-down): the VM itself isn't reachable. Re-raised as
      ``ConnectivityError`` so the operator sees "VM unreachable" rather
      than "agent needs reinit."
    """
    from agentworks.errors import ConnectivityError
    from agentworks.ssh import SSH_TRANSPORT_ERROR, SSHError

    try:
        probe = target.run("true", check=False)
    except SSHError as e:
        raise ConnectivityError(
            f"direct SSH probe to agent '{agent.name}' failed: {e}",
            entity_kind="agent",
            entity_name=agent.name,
            hint=f"Check that VM '{agent.vm_name}' is reachable.",
        ) from e
    if probe.ok:
        return
    # SSH transport failures (auth rejected, host unreachable, etc.) report
    # SSH_TRANSPORT_ERROR (255). Combined with no other obvious signal, this
    # is our best indication that direct agent SSH is not yet provisioned.
    if probe.returncode == SSH_TRANSPORT_ERROR:
        raise StateError(
            f"agent '{agent.name}' rejected direct SSH (likely predates the "
            "direct-target-user-SSH rollout).",
            entity_kind="agent",
            entity_name=agent.name,
            hint=f"Run 'agw agent reinit {agent.name}' to populate its authorized_keys.",
        )


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

    from agentworks.ssh import agent_exec_target, interactive

    # Direct agent SSH (FRD R1): no admin+sudo detour. The agent's
    # authorized_keys (Phase 3) accepts the operator's key set.
    target = agent_exec_target(vm, config, agent)

    with keep_vm_active(db, config, vm):
        # Probe direct agent SSH first so pre-rollout agents (whose
        # authorized_keys was never populated) get an actionable error
        # rather than dropping into a remote shell that immediately exits
        # on Permission denied.
        _assert_agent_ssh_works(target, agent)

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
            # SSH as the agent, then cd into the workspace and exec an
            # interactive login shell. No sudo / su involved.
            shell_cmd = f"cd {q_path} && exec $SHELL -li"
            sys.exit(interactive(target, shell_cmd))
        else:
            # SSH as the agent with no command -> interactive login shell.
            sys.exit(interactive(target, ""))


def exec_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    command: list[str],
) -> int:
    """Execute a command as an agent user on a VM via direct agent SSH.

    Opens a non-interactive SSH session directly as the agent's Linux user
    (FRD R1) and runs the command in a login shell so the agent's PATH /
    profile is in scope. Stdout / stderr stream through to the caller; the
    return value is the remote command's exit code.
    """
    import shlex

    from agentworks.ssh import agent_exec_target

    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    vm = _require_vm(db, agent.vm_name)
    target = agent_exec_target(vm, config, agent)

    with keep_vm_active(db, config, vm):
        # Probe direct agent SSH first so pre-rollout agents (whose
        # authorized_keys was never populated) get an actionable error.
        _assert_agent_ssh_works(target, agent)

        remote_cmd = command[0] if len(command) == 1 else shlex.join(command)
        # Wrap in a login shell so the agent's PATH (mise shims,
        # ~/.local/bin, etc.) is set up. This matches the env an operator
        # gets via `agent shell`.
        return target.call_streaming(f"$SHELL -lc {shlex.quote(remote_cmd)}")


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
    logger: SSHLogger,
) -> None:
    """Create an agent Linux user on a VM and set up their environment.

    Workspace group membership is NOT set here - it is managed by the grant
    system. This function only creates the user and configures their tools.

    Two SSH transports are used in sequence:

    - ``admin_target`` for root-level work that the agent cannot do for
      itself (useradd, socket-root setup, writing files INTO the agent's
      home via sudo+chown). Used unconditionally throughout.
    - ``agent_target`` for "do work AS the agent" steps (git config,
      dotfiles install, install commands, mise setup, claude plugins). Built
      AFTER ``_reconcile_authorized_keys`` populates the agent's
      authorized_keys; from that point on, direct agent SSH is available
      and FRD R1's "operations whose target user is the agent open SSH
      directly as the agent's Linux user" applies.
    """
    from agentworks.ssh import exec_target_for_user

    admin_target = admin_exec_target(vm, config, logger=logger)

    output.detail(f"Creating user '{linux_user}' on VM '{vm.name}'...")
    home = f"/home/{linux_user}"

    agent_cfg = config.agent
    agent_shell = agent_cfg.shell

    # Create user with the template's shell (idempotent: skip if exists)
    shell_path = f"/bin/{agent_shell}" if "/" not in agent_shell else agent_shell
    user_exists = admin_target.run(f"id {linux_user}", sudo=True, check=False)
    if not user_exists.ok:
        admin_target.run(f"useradd -m -s {shell_path} {linux_user}", sudo=True)
    else:
        admin_target.run(f"usermod -s {shell_path} {linux_user}", sudo=True)

    # Reconcile the agent's authorized_keys so direct SSH as the agent works
    # (FRD R3). Identical call shape at create and reinit (declarative
    # parity); the helper handles the agent's ~/.ssh creation on first run.
    # This MUST run before agent_target is used below.
    from agentworks.vms.initializer import _reconcile_authorized_keys

    _reconcile_authorized_keys(
        admin_target,
        config,
        home=home,
        logger=logger,
        owner=linux_user,
    )

    # Direct agent SSH is now available (just populated the keys above).
    # Everything downstream that runs AS the agent uses agent_target;
    # admin_target stays in scope for the few remaining root-only steps
    # (socket-root setup, writing files into agent home via sudo+chown).
    agent_target = exec_target_for_user(vm, config, user=linux_user, logger=logger)

    # Ensure the agent tmux socket infrastructure exists. Call
    # ensure_agent_socket_root first so this works on VMs that haven't
    # been reinited since the socket feature was added.
    from agentworks.sessions.tmux import cleanup_stale_sockets, ensure_agent_socket_dir, ensure_agent_socket_root

    ensure_agent_socket_root(admin_target, vm.admin_username)
    # The per-agent dir won't exist for a brand-new agent -- suppress the
    # "missing" warning. Misconfiguration of an existing dir still warns.
    ensure_agent_socket_dir(admin_target, linux_user, warn_if_missing=False)
    removed = cleanup_stale_sockets(admin_target, linux_user)
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
        _write_agent_file(admin_target, linux_user, rc_file, rc_content)

    # Git safe.directory wildcard (agents access repos owned by admin)
    if config.admin.git_force_safe_directory:
        try:
            agent_target.run("git config --global --add safe.directory '*'")
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
                _write_agent_file(admin_target, linux_user, f"{home}/.git-credentials", cred_content, mode="0600")
                agent_target.run("git config --global credential.helper store")
        except Exception as e:
            output.warn(f"agent git credential setup failed: {e}")

    # User install commands for the agent
    _run_agent_install_commands(
        admin_target=admin_target,
        agent_target=agent_target,
        config=config,
        linux_user=linux_user,
        home=home,
    )

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
                                check=False, timeout=120,
                            )
                            checkout = agent_target.run(
                                f"git -C {_shlex.quote(dest)} checkout {_shlex.quote(ref.ref)}",
                                check=False,
                            )
                            if not checkout.ok:
                                output.warn(
                                    f"dotfiles checkout of '{ref.ref}' failed, skipping"
                                )
                        else:
                            pull = agent_target.run(
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
                    agent_target.run(clone_cmd, timeout=120)
            else:
                # Local source: copy as admin then chown to the agent.
                tmp_dotfiles = f"/tmp/agentworks-{linux_user}-dotfiles"
                admin_target.run(f"rm -rf {tmp_dotfiles}", check=False)
                from agentworks.sources import fetch_dir

                fetch_dir(ref, admin_target, tmp_dotfiles)
                admin_target.run(f"mv {tmp_dotfiles} {dest}", sudo=True)
                admin_target.run(f"chown -R {linux_user}:{linux_user} {dest}", sudo=True)

            output.detail(f"Running agent dotfiles install: {agent_cfg.dotfiles_install_cmd}")
            agent_target.run(
                f"cd {dest} && {agent_cfg.dotfiles_install_cmd}",
                timeout=120,
            )
        except (SourceRefError, Exception) as e:
            output.warn(f"agent dotfiles failed: {e}")

    # Mise for the agent
    _run_agent_mise_setup(
        admin_target=admin_target,
        agent_target=agent_target,
        config=config,
        linux_user=linux_user,
        home=home,
    )

    # Install nerf Claude plugin for the agent
    if config.agent.nerf_install_claude_plugin:
        _install_nerf_claude_plugin_for_agent(agent_target, agent_shell)

    # Claude Code marketplaces and plugins for the agent
    from agentworks.vms.initializer import install_claude_plugins

    install_claude_plugins(
        lambda cmd, timeout: agent_target.run(cmd, timeout=timeout),
        config.agent.claude_marketplaces,
        config.agent.claude_plugins,
    )


def _install_nerf_claude_plugin_for_agent(
    agent_target: ExecTarget,
    shell: str,
) -> None:
    """Install the nerf Claude Code plugin for an agent user. Non-fatal."""
    from agentworks.ssh import SSHError

    try:
        check = agent_target.run(
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
        agent_target.run(
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
    *,
    admin_target: ExecTarget,
    agent_target: ExecTarget,
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
    from agentworks.ssh import SSHError

    catalog = load_catalog(config)
    shell = config.agent.shell
    path_additions: list[str] = []
    total = len(command_names)

    for i, name in enumerate(command_names, 1):
        entry = catalog.user_install_commands.get(name)
        if entry is None:
            output.warn(f"install command '{name}' not found in catalog")
            continue
        # Skip if already installed for this user (short timeout)
        test_cmd = _build_agent_test_command(entry, home, shell)
        if test_cmd:
            try:
                check = agent_target.run(test_cmd, check=False, timeout=10)
                if check.returncode == 0:
                    output.detail(f"Agent install command {i}/{total} ({name}): already installed, skipping")
                    path_additions.extend(entry.path)
                    continue
            except SSHError as e:
                output.warn(f"install check for '{name}' failed ({e}), assuming not installed")

        truncated = entry.command[:60]
        output.detail(f"Agent install command {i}/{total} ({name}): {truncated}...")
        try:
            # Run the install command in a login shell to source the agent's profile.
            agent_target.run(
                f"{shell} -lc {shlex.quote(entry.command)}",
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
            _write_agent_file(admin_target, linux_user, profile_path, content)
            # Source from shell profiles (the agent owns these files; write directly).
            source_line = f". {profile_path}"
            rc_files = [f"{home}/.profile", f"{home}/.bashrc"]
            if shell == "zsh":
                rc_files.append(f"{home}/.zprofile")
            for rc in rc_files:
                agent_target.run(
                    f"grep -q {AGENTWORKS_PROFILE} {rc} 2>/dev/null || printf '%s\\n' '{source_line}' >> {rc}",
                )
        except SSHError as e:
            output.warn(f"agent PATH configuration failed: {e}")


def _run_agent_mise_setup(
    *,
    admin_target: ExecTarget,
    agent_target: ExecTarget,
    config: Config,
    linux_user: str,
    home: str,
) -> None:
    """Set up mise for an agent: shims PATH, config, lockfile, install."""
    from agentworks.ssh import SSHError

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

    # Write mise activation to agent's rc (interactive shell hooks)
    if agent_cfg.mise_activate:
        try:
            rc_path = f"{home}/{AGENTWORKS_RC}"
            rc_content = f"# Managed by agentworks -- do not edit\n{MISE_ACTIVATE_LINES}\n"
            _write_agent_file(admin_target, linux_user, rc_path, rc_content)
            source_line = f". {rc_path}"
            for rc in [f"{home}/.bashrc", f"{home}/.zshrc"]:
                agent_target.run(
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
            agent_target.run(f"mkdir -p {mise_config_dir}")
            _write_agent_file(admin_target, linux_user, f"{mise_config_dir}/config.toml", mise_config)
        except SSHError as e:
            output.warn(f"agent mise config write failed: {e}")
            return

    # Copy lockfile if configured
    if has_lockfile and agent_cfg.mise_lockfile:
        output.detail(f"Fetching agent mise lockfile from {agent_cfg.mise_lockfile}...")
        try:
            from agentworks.sources import SourceRefError, fetch_file, parse_source_ref

            ref = parse_source_ref(agent_cfg.mise_lockfile, default_filename="mise.lock")
            agent_target.run(f"mkdir -p {mise_config_dir}")
            # Fetch to tmp via admin (network egress may need admin creds), then
            # move into agent home via sudo+chown.
            tmp_lock = f"/tmp/agentworks-{linux_user}-mise-lock"
            fetch_file(ref, admin_target, tmp_lock)
            admin_target.run(f"mv {tmp_lock} {mise_config_dir}/mise.lock", sudo=True)
            admin_target.run(f"chown {linux_user}:{linux_user} {mise_config_dir}/mise.lock", sudo=True)
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
        agent_target.run(f"mise install {install_flags}", timeout=300)
        output.detail("Agent mise packages installed")
        installed = True
    except SSHError as e:
        if lockfile_exists and agent_cfg.mise_allow_unlocked:
            output.warn("some agent packages not in lockfile, installing unlocked...")
            try:
                agent_target.run("mise install -y", timeout=300)
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
            agent_target.run("mise prune -y", timeout=60)


def _build_agent_test_command(
    entry: UserInstallCommandEntry,
    home: str,
    shell: str,
) -> str | None:
    """Build a test command that runs as the agent user.

    The caller runs this via the agent's ExecTarget. ``test_exec`` checks
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
