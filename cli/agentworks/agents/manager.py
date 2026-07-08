"""Agent lifecycle orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

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
from agentworks.transports import transport
from agentworks.vms.manager import keep_vm_active

if TYPE_CHECKING:
    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.catalog import UserInstallCommandEntry
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, VMRow, WorkspaceRow
    from agentworks.env import EnvEntry
    from agentworks.resources import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.ssh import SSHLogger
    from agentworks.transports import Transport

AGENT_PREFIX = "agt-"
WS_GROUP_PREFIX = "ws-"


def derive_linux_user(agent_name: str) -> str:
    """Derive the Linux username for a newly-created agent: agt-<name>.

    Existing agents retain whatever username was stored in the database at
    their creation time (older agents use the legacy agt-- prefix). Always
    read agent_row.linux_user for the canonical value; this helper is only
    used at agent-create time.
    """
    return f"{AGENT_PREFIX}{agent_name}"


class _AgentDirectEnvScopes(NamedTuple):
    """Per-scope env dicts for ``agent shell`` / ``agent exec``.

    The ``workspace`` field is ``None`` for shells / execs that don't
    pin a workspace context (``agent shell`` without ``--workspace``,
    ``agent exec`` today). When set, workspace-template env enters the
    FRD R2 precedence ladder between vm and agent.
    """

    vm: dict[str, EnvEntry]
    workspace: dict[str, EnvEntry] | None
    agent: dict[str, EnvEntry]


def _resolve_agent_direct_env_scopes(
    registry: Registry,
    vm: VMRow,
    agent: AgentRow,
    *,
    ws: WorkspaceRow | None = None,
) -> _AgentDirectEnvScopes:
    """Resolve per-scope env dicts for ``agent shell`` / ``agent exec``.

    Both the SecretTarget (eager-resolve) and the ``compose_env`` call
    (render) consume the result of this helper, guaranteeing they see
    identical scope state -- no drift between "what was prompted for"
    and "what was passed to the shell."

    Scope sources mirror the FRD R2 precedence ladder:

    - ``vm``: the VM's actual template env (from the ``vm.template`` DB row).
    - ``workspace``: when ``ws`` is supplied, the workspace template's env.
    - ``agent``: the agent row's template env (from the DB row).
      The agent pre-exists this call and may have been created under a
      different template than the operator's current ``--template``
      would resolve.
    """
    from agentworks.agents.templates import resolve_template as _resolve_agent_template
    from agentworks.vms.templates import resolve_template as _resolve_vm_template
    from agentworks.workspaces.templates import resolve_template as _resolve_ws_template

    vm_tmpl = _resolve_vm_template(registry, vm.template)
    agent_tmpl = _resolve_agent_template(registry, agent.template)
    ws_env: dict[str, EnvEntry] | None = None
    if ws is not None:
        ws_env = _resolve_ws_template(registry, ws.template).env
    return _AgentDirectEnvScopes(
        vm=vm_tmpl.env,
        workspace=ws_env,
        agent=agent_tmpl.env,
    )


def _agent_direct_secret_target(
    scopes: _AgentDirectEnvScopes, *, label: str,
) -> SecretTarget:
    """Build the SecretTarget for ``agent shell`` / ``agent exec`` from
    pre-resolved scope dicts.

    Single-phase: the operator opens one shell as the agent's Linux user.
    The companion ``compose_env`` call must consume the same ``scopes``
    so the eager-resolve prompts cover exactly what the runtime env will
    reference (no drift).
    """
    from agentworks.secrets import SecretTarget

    return SecretTarget(
        vm=scopes.vm,
        workspace=scopes.workspace,
        agent=scopes.agent,
        label=label,
    )


def _resolve_workspace_for_agent(
    db: Database, vm: VMRow, agent: AgentRow, workspace_name: str | None,
) -> WorkspaceRow | None:
    """Resolve a ``--workspace`` flag for ``agent shell`` / ``agent exec``.

    Returns ``None`` when ``workspace_name`` is ``None``. Otherwise loads
    the workspace and validates (in order) that it exists, belongs to the
    agent's VM, and the agent has access. All failures surface as clean
    typed errors before any SSH work.
    """
    if workspace_name is None:
        return None
    ws = db.get_workspace(workspace_name)
    if ws is None:
        raise NotFoundError(
            f"workspace '{workspace_name}' not found",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    if ws.vm_name != vm.name:
        raise ValidationError(
            f"workspace '{workspace_name}' belongs to VM '{ws.vm_name}', not '{vm.name}'",
            entity_kind="workspace",
            entity_name=workspace_name,
        )
    if not db.has_any_grant(agent.name, workspace_name):
        raise AuthorizationError(
            f"agent '{agent.name}' does not have access to workspace '{workspace_name}'",
            entity_kind="agent",
            entity_name=agent.name,
            hint=f"Run 'agent grant-workspaces {agent.name} {workspace_name}' to grant access.",
        )
    return ws


def workspace_group(workspace_name: str) -> str:
    """Derive the Linux group name for a newly-created workspace: ws-<name>.

    Existing workspaces retain whatever group was stored in the database at
    their creation time (legacy workspaces use the older ws-- prefix).
    Always read workspace_row.linux_group for the canonical value; this
    helper is only used at workspace-create time.
    """
    return f"{WS_GROUP_PREFIX}{workspace_name}"


def _assert_agent_ssh_works(target: Transport, agent: AgentRow) -> None:
    """Probe direct agent SSH; raise an actionable error on auth rejection.

    The direct-target-user-SSH rollout populates each agent's
    ``~/.ssh/authorized_keys`` with the operator's key set at agent create /
    reinit. Agents that existed before this rollout have a home directory
    with no ``.ssh/authorized_keys`` for the operator, so direct SSH as the
    agent is rejected. Catch that specific case here and turn the otherwise-
    opaque SSH transport failure into a clear "run ``agw agent reinit``"
    instruction.

    A probe round-trip is cheap relative to letting the failure surface
    mid-operation with partial state.

    Two failure shapes are distinguished:

    - Non-zero exit (SSH_TRANSPORT_ERROR = 255 typically): SSH connected
      and ``ssh`` itself reported an auth / transport failure. Treated as
      the pre-rollout case and raised as ``StateError`` with a reinit hint.
    - ``SSHError`` from ``target.run`` (timeout / unreachable host): the
      VM itself isn't reachable. Re-raised as ``ConnectivityError`` so the
      operator sees "VM unreachable" rather than "agent needs reinit."
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

    from agentworks.agents.templates import resolve_template
    from agentworks.bootstrap import build_registry

    # build_registry runs first so framework miss-policies (e.g.
    # GitCredentialKind's error policy on agent template's
    # git_credentials list, future TemplateReference typos on
    # inherits) fire before any template / DB / VM business logic
    # surfaces its own NotFoundError.
    registry = build_registry(config)

    agent_tmpl = resolve_template(registry, template)

    validate_name(name)

    if db.get_agent(name) is not None:
        raise AlreadyExistsError(
            f"agent '{name}' already exists",
            entity_kind="agent",
            entity_name=name,
        )

    vm = _require_vm(db, vm_name)
    linux_user = derive_linux_user(name)

    # Collect agent-provisioning credentials (git tokens live outside
    # the env-block system). Operator env secrets are NOT prompted at
    # agent create -- provisioning is hermetic. They get prompted at
    # the use site (agent shell, session create, etc.).
    git_tokens = _collect_agent_credentials(config, registry, agent_tmpl)

    from agentworks.ssh import SSHLogger
    ssh_logger = SSHLogger(vm.name, "agent-create")
    output.info(
        f"Creating agent '{name}' on VM '{vm_name}' (template: {agent_tmpl.name})..."
    )
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
                _create_agent_on_vm(
                    vm, config, registry, agent_tmpl, linux_user,
                    agent_name=name,
                    git_tokens=git_tokens,
                    logger=ssh_logger,
                )
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
    output.info(f"Deleting agent '{name}' on VM '{vm.name}'...")
    with keep_vm_active(db, config, vm):

        # Kill running sessions for this agent (status-aware)
        if agent_sessions:
            from agentworks.db import SessionStatus
            from agentworks.sessions.manager import check_session_status, ensure_pids_batch
            from agentworks.sessions.tmux import force_kill_tmux_server, kill_session

            target = transport(vm, config, logger=ssh_logger)
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

    from agentworks.agents.templates import resolve_template
    from agentworks.bootstrap import build_registry

    # build_registry runs first so framework miss-policies fire before
    # template / DB / VM business logic.
    registry = build_registry(config)

    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    agent_tmpl = resolve_template(registry, agent.template)

    vm = _require_vm(db, agent.vm_name)

    # Collect credentials up front before any SSH work.
    git_tokens = _collect_agent_credentials(config, registry, agent_tmpl)

    # Provisioning is hermetic: no operator-env secrets are prompted at
    # agent reinit. They get prompted at the use site (agent shell,
    # session create, etc.) once reinit completes.

    from agentworks.ssh import SSHLogger
    ssh_logger = SSHLogger(vm.name, "agent-reinit")
    with keep_vm_active(db, config, vm):
        try:
            try:
                _create_agent_on_vm(
                    vm, config, registry, agent_tmpl, agent.linux_user,
                    agent_name=agent.name,
                    git_tokens=git_tokens,
                    logger=ssh_logger,
                )
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
    names_only: bool = False,
) -> None:
    """List agents.

    With ``names_only=True``, emit one agent name per line and skip
    the table render. Used by shell completion (see issue #147).
    """
    agents = db.list_agents(vm_name=vm_name)

    if names_only:
        # Empty / fully-filtered-out result prints nothing under
        # names-only; the friendly "No agents found" line below is
        # for human readers only.
        for agent in agents:
            output.info(agent.name)
        return

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

    from agentworks.env import ResourceContext, compose_env
    from agentworks.secrets import resolve_for_command
    from agentworks.transports import agent_transport

    # Resolve workspace upfront (needed for authz check, env scope, AND
    # ctx) before any SSH probe so failures surface as clean validation
    # errors and the eager-resolve below sees the right scope chain.
    ws = _resolve_workspace_for_agent(db, vm, agent, workspace_name)

    # Eager-prompting orchestration (FRD R4 / Phase 6): resolve every
    # secret referenced by the agent shell's env chain BEFORE opening
    # the interactive SSH session. The same scope dicts feed both the
    # SecretTarget (for resolve_for_command) and compose_env below so
    # the two consumers can't drift.
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    scopes = _resolve_agent_direct_env_scopes(registry, vm, agent, ws=ws)
    values = resolve_for_command(
        [_agent_direct_secret_target(scopes, label=f"agent-shell={agent.name}")],
        config,
        registry,
    )

    ctx = ResourceContext(
        vm_name=vm.name,
        vm_host=vm.vm_host_name,
        platform=vm.platform,
        user=agent.linux_user,
        workspace_name=ws.name if ws else None,
        workspace_dir=ws.workspace_path if ws else None,
        agent_name=agent.name,
    )
    env = compose_env(
        values=values,
        ctx=ctx,
        vm=scopes.vm,
        workspace=scopes.workspace,
        agent=scopes.agent,
    )

    # Direct agent SSH (FRD R1): no admin+sudo detour. The agent's
    # authorized_keys (Phase 3) accepts the operator's key set.
    target = agent_transport(vm, config, agent)

    with keep_vm_active(db, config, vm):
        # Probe direct agent SSH first so pre-rollout agents (whose
        # authorized_keys was never populated) get an actionable error
        # rather than dropping into a remote shell that immediately exits
        # on Permission denied.
        _assert_agent_ssh_works(target, agent)

        if ws is not None:
            import shlex

            q_path = shlex.quote(ws.workspace_path)
            # SSH as the agent, then cd into the workspace and exec an
            # interactive login shell. No sudo / su involved.
            shell_cmd = f"cd {q_path} && exec $SHELL -li"
            sys.exit(target.interactive(shell_cmd, env=env))
        else:
            # SSH as the agent with no command -> interactive login shell.
            sys.exit(target.interactive("", env=env))


def exec_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    command: list[str],
    workspace_name: str | None = None,
) -> int:
    """Execute a command as an agent user on a VM via direct agent SSH.

    Opens a non-interactive SSH session directly as the agent's Linux user
    (FRD R1) and runs the command in a login shell so the agent's PATH /
    profile is in scope. Stdout / stderr stream through to the caller; the
    return value is the remote command's exit code.

    When ``workspace_name`` is set, the command runs from the workspace
    directory and the workspace template's env joins the env chain. The
    workspace must belong to the agent's VM and the agent must have
    access.
    """
    import shlex

    from agentworks.env import ResourceContext, compose_env
    from agentworks.exec_validation import reject_dash_prefixed_command
    from agentworks.secrets import resolve_for_command
    from agentworks.transports import agent_transport

    reject_dash_prefixed_command(command, kind="agent", name=name)

    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    vm = _require_vm(db, agent.vm_name)

    # Resolve workspace upfront so cross-VM / authz failures surface as
    # clean typed errors before any SSH work and the eager-resolve below
    # sees the right scope chain.
    ws = _resolve_workspace_for_agent(db, vm, agent, workspace_name)

    # Eager-prompting orchestration (FRD R4 / Phase 6): resolve every
    # secret referenced by the agent exec env chain BEFORE running the
    # remote command. The same scope dicts feed both the SecretTarget
    # and compose_env below so the two consumers can't drift.
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    scopes = _resolve_agent_direct_env_scopes(registry, vm, agent, ws=ws)
    values = resolve_for_command(
        [_agent_direct_secret_target(scopes, label=f"agent-exec={agent.name}")],
        config,
        registry,
    )

    ctx = ResourceContext(
        vm_name=vm.name,
        vm_host=vm.vm_host_name,
        platform=vm.platform,
        user=agent.linux_user,
        workspace_name=ws.name if ws else None,
        workspace_dir=ws.workspace_path if ws else None,
        agent_name=agent.name,
    )
    env = compose_env(
        values=values,
        ctx=ctx,
        vm=scopes.vm,
        workspace=scopes.workspace,
        agent=scopes.agent,
    )

    target = agent_transport(vm, config, agent)

    with keep_vm_active(db, config, vm):
        # Probe direct agent SSH first so pre-rollout agents (whose
        # authorized_keys was never populated) get an actionable error.
        _assert_agent_ssh_works(target, agent)

        remote_cmd = command[0] if len(command) == 1 else shlex.join(command)
        if ws is not None:
            remote_cmd = f"cd {shlex.quote(ws.workspace_path)} && {remote_cmd}"
        # Wrap in a login shell so the agent's PATH (mise shims,
        # ~/.local/bin, etc.) is set up. This matches the env an operator
        # gets via `agent shell`.
        return target.call_streaming(
            f"$SHELL -lc {shlex.quote(remote_cmd)}", env=env,
        )


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
    target = transport(vm, config, logger=logger)
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
    target = transport(vm, config, logger=logger)
    ws_grp = _resolve_ws_group(db, workspace_name)
    target.run(f"gpasswd -d {linux_user} {ws_grp}", sudo=True, check=False)


def _collect_agent_credentials(
    config: Config,
    registry: Registry,
    agent_tmpl: ResolvedAgentTemplate,
) -> dict[str, str]:
    """Collect git credentials up front before any SSH work begins.

    Phase 1d of the Resource Registry SDD: tokens flow through the
    framework (``_collect_git_tokens`` walks each credential's
    ``token`` field, resolves through the backend chain, returns the
    ``{credential_name: value}`` map). The legacy provider-side
    resolution path is gone. The registry is built upstream so its
    finalize-pass typo errors fire before any other precondition.
    """
    agent_cfg = agent_tmpl
    if not agent_cfg.git_credentials:
        return {}

    from agentworks.vms.manager import _collect_git_tokens

    return _collect_git_tokens(config, registry, agent_cfg.git_credentials)


def _create_agent_on_vm(
    vm: VMRow,
    config: Config,
    registry: Registry,
    agent_tmpl: ResolvedAgentTemplate,
    linux_user: str,
    *,
    agent_name: str,
    git_tokens: dict[str, str] | None = None,
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
    agent's behalf and matches FRD R1's "operations whose target user is
    the agent open SSH directly as the agent's Linux user."
    """
    from agentworks.sessions.tmux import (
        cleanup_stale_sockets,
        ensure_agent_socket_dir,
        ensure_agent_socket_root,
    )
    from agentworks.transports import transport_for_user
    from agentworks.vms.initializer import _reconcile_authorized_keys

    admin_target = transport(vm, config, logger=logger)

    output.detail(f"Creating user '{linux_user}' on VM '{vm.name}'...")
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
        output.detail(f"Cleaned up {removed} stale socket(s)")

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
    # accumulated PATH entries from catalog install commands.
    from agentworks.env import ResourceContext, per_user_identity_env

    agent_identity_ctx = ResourceContext(
        vm_name=vm.name,
        platform=vm.platform,
        user=linux_user,
        vm_host=vm.vm_host_name,
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
    from agentworks.resources.access import admin_template as _admin_template

    if _admin_template(registry).git_force_safe_directory:
        try:
            agent_target.run("git config --global --add safe.directory '*'")
            output.detail("Git safe.directory configured for agent")
        except Exception as e:
            output.warn(f"agent git safe.directory setup failed: {e}")

    # Git credentials for the agent (tokens pre-resolved by the
    # framework upstream in agents/manager.create_agent / reinit_agent
    # via _collect_agent_credentials). Phase 1d invariant: if the
    # agent template declares git_credentials, the caller MUST have
    # resolved every token; a missing entry is a caller bug and
    # raises loudly rather than shipping a VM with a silently-dropped
    # credential the operator asked for.
    if agent_cfg.git_credentials:
        from agentworks.vms.initializer import resolve_git_credential_providers

        output.detail("Configuring git credentials for agent...")
        providers = resolve_git_credential_providers(registry, agent_cfg.git_credentials)
        missing = [
            cred_name for cred_name in providers
            if not git_tokens or cred_name not in git_tokens
        ]
        if missing:
            from agentworks.errors import StateError

            raise StateError(
                f"agent git credential setup: token(s) not resolved by "
                f"the framework for {missing!r}; caller must pre-resolve "
                f"every provider's token before invoking this function",
                entity_kind="git-credential",
                entity_name=missing[0],
            )
        assert git_tokens is not None  # missing-check above narrows it
        from agentworks.git_credentials import (
            GIT_CRED_WARN_HELPER_PATH,
            GIT_SCOPES_INCLUDE_PATH,
            build_credential_materials,
        )

        # Same materials as the VM-level (admin) flow: store lines with
        # the unscoped-first ordering contract, gitconfig context
        # sections for scoped credentials, and the warn-only helper.
        # ``git config --global`` runs as the agent user, so the
        # tilde-literal include.path resolves to the agent's home; the
        # write_file paths spell the home out (agent_target conventions).
        materials = build_credential_materials(providers, git_tokens)
        agent_target.write_file(
            f"{home}/.git-credentials", materials.store_content, mode="0600"
        )
        agent_target.write_file(
            f"{home}/{GIT_SCOPES_INCLUDE_PATH.removeprefix('~/')}",
            materials.gitconfig_content,
            mode="0600",
        )
        agent_target.write_file(
            f"{home}/{GIT_CRED_WARN_HELPER_PATH.removeprefix('~/')}",
            materials.warn_helper_script,
            mode="0700",
        )
        agent_target.run(
            "git config --global credential.helper store && "
            f"(git config --global --get-all include.path | grep -qxF '{GIT_SCOPES_INCLUDE_PATH}' "
            f"|| git config --global --add include.path '{GIT_SCOPES_INCLUDE_PATH}')"
        )

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
        output.detail(f"Syncing agent dotfiles from {agent_cfg.dotfiles_source}...")
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
                # Local source: fetch directly into the agent's home over
                # agent SSH. The agent owns dest, so no sudo / chown
                # dance. fetch_dir handles existing-dest overwrite.
                fetch_dir(ref, agent_target, dest)

            output.detail(f"Running agent dotfiles install: {agent_cfg.dotfiles_install_cmd}")
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
            f"{agent_shell} -lc {_shlex.quote(cmd)}", timeout=timeout,
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
        agent_target, home=home, shell=agent_shell, logger=logger,
    )


def _delete_agent_on_vm(
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

    Runs entirely over agent SSH (FRD R1). The agent owns its home, so
    the profile fragment is written via ``agent_target.write_file``
    directly, with no sudo / chown dance.

    The profile fragment is rewritten unconditionally (even when there
    are no install commands and no PATH additions) so that reinit can
    clear previously set paths. Catalog install commands add their
    ``path`` entries on top.

    Install commands run without any env= injection -- provisioning is
    hermetic. Static identity (``AGENTWORKS_AGENT``, etc.) reaches the
    install command via login-shell sourcing of the per-user profile
    fragment that was written earlier in agent-setup phase 2.
    """
    import shlex

    from agentworks.catalog import catalog_from_registry
    from agentworks.ssh import SSHError

    catalog = catalog_from_registry(registry)
    shell = agent_tmpl.shell
    path_additions: list[str] = []
    command_names = agent_tmpl.user_install_commands
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
    # in _create_agent_on_vm (so install commands above could see
    # AGENTWORKS_AGENT via login-shell sourcing); the rewrite here adds
    # the PATH entries those install commands contributed.
    if path_additions:
        output.detail(f"Adding {len(path_additions)} PATH entries for agent...")
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
    for p in (path_additions or []):
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

    Runs entirely over agent SSH (FRD R1). Writes the mise config and
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
            agent_target.write_file(f"{mise_config_dir}/config.toml", mise_config, mode="0644")
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
        output.detail("Agent mise packages installed")
        installed = True
    except SSHError as e:
        if lockfile_exists and agent_cfg.mise_allow_unlocked:
            output.warn("some agent packages not in lockfile, installing unlocked...")
            try:
                agent_target.run(
                    f"{agent_shell} -lc {shlex.quote('mise install -y')}",
                    timeout=300,
                )
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
