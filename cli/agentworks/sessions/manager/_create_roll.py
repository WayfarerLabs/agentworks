"""``_roll_forward``: the dependency-ordered mutation phase of ``create_session``.

Section S11, running inside the activation gate after the boundary
resolve. Split into three functions that preserve the original two-level
rollback exactly:

- :func:`_realize_ephemerals` realizes the pending workspace / agent in
  dependency order, recording each in the :class:`RealizationLog`.
- :func:`_start_session_slice` is the session's own realizing slice: the
  grant, the DB row, the tmux launch, and the completion bookkeeping.
  Its inner ``except`` runs the session node's partial-state teardown
  (rollback level 1).
- :func:`_roll_forward` frames both in the outer try whose ``except``
  unwinds the realized ephemerals (rollback level 2). A COMPLETED
  session (tmux up, node flipped) is deliberately never rolled back.

The bodies are the original ``create_session`` roll-forward moved
verbatim: no emit, DB call, SSH call, or teardown was reordered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.sessions.manager as _mgr
from agentworks import output
from agentworks.db import SessionMode
from agentworks.errors import NotFoundError, ValidationError

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, VMRow
    from agentworks.orchestration.unwind import RealizationLog
    from agentworks.resources.registry import Registry
    from agentworks.sessions.tmux import RunCommand
    from agentworks.transports import Transport

    from ._create_types import SessionGraph, SessionPlan


def _realize_ephemerals(
    db: Database,
    config: Config,
    registry: Registry,
    plan: SessionPlan,
    graph: SessionGraph,
    vm: VMRow,
    secret_values: dict[str, str],
    log: RealizationLog,
) -> None:
    """Realize the pending workspace / agent in dependency order, recording
    each completed realization in ``log`` for the reverse-order unwind."""
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.secrets import ScopedSecrets

    def scoped_ctx(secret_names: tuple[str, ...]) -> RunContext:
        return RunContext(
            config=config,
            operation_scope=graph.scope,
            secrets=ScopedSecrets(secret_values, secret_names),
        )

    if graph.pending_workspace is not None:
        from agentworks.workspaces.realize import realize_workspace

        assert graph.workspace_tmpl is not None  # resolved at build above
        # The realizer emits its own "Creating workspace ..." line
        # (used by the standalone `workspace create` path too), so the
        # session flow must not echo it a second time here.
        with output.section("Creating Workspace"):
            realize_workspace(
                db,
                config,
                registry,
                name=plan.workspace_name,
                vm=vm,
                template=graph.workspace_tmpl,
            )
            log.mark_realized(graph.pending_workspace)
    if graph.pending_agent is not None:
        from agentworks.agents.nodes import credential_tokens
        from agentworks.agents.realize import realize_agent

        assert plan.agent_name is not None  # defaulted to ``name`` above
        assert graph.agent_tmpl is not None and graph.agent_tmpl_node is not None
        with output.section("Creating Agent"):
            output.info(f"Creating agent '{plan.agent_name}' on VM '{vm.name}' (template: {graph.agent_tmpl.name})...")
            # Each credential's token, read through its node's SCOPED
            # delivery (the boundary pass above covered them; the
            # graph-derived fold replaces the nested create_agent's
            # git_tokens hand-off).
            git_tokens = credential_tokens(graph.agent_tmpl_node, scoped_ctx)
            realize_agent(
                db,
                config,
                registry,
                name=plan.agent_name,
                vm=vm,
                template=graph.agent_tmpl,
                git_tokens=git_tokens,
            )
            log.mark_realized(graph.pending_agent)


def _start_session_slice(
    db: Database,
    config: Config,
    registry: Registry,
    plan: SessionPlan,
    graph: SessionGraph,
    vm: VMRow,
    target: Transport,
    run_command: RunCommand,
    agent_target: Transport | None,
    secret_values: dict[str, str],
) -> None:
    """The session's own realizing slice: the grant, the DB row, the tmux
    launch, and the completion bookkeeping.

    The inner ``except`` runs the session node's partial-state teardown
    (rollback level 1); a completed session is then flipped and never
    rolled back. The terminal result line and the post-start bookkeeping
    render at column 0, outside the ``Starting Session`` section.
    """
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.secrets import ScopedSecrets
    from agentworks.sessions.tmux import (
        create_session as create_tmux_session,
    )
    from agentworks.sessions.tmux import (
        deploy_restricted_config,
    )

    name = plan.name
    workspace_name = plan.workspace_name
    agent_name = plan.agent_name
    scope = graph.scope
    template = graph.template
    session_node = graph.session_node
    pending_workspace = graph.pending_workspace
    pending_agent = graph.pending_agent

    ws = _mgr._require_workspace(db, workspace_name)

    resolved_agent_name: str | None = None
    agent_row: AgentRow | None = None
    if agent_name is not None:
        mode = SessionMode.AGENT
        agent_row = db.get_agent(agent_name)
        if agent_row is None:
            raise NotFoundError(
                f"agent '{agent_name}' not found",
                entity_kind="agent",
                entity_name=agent_name,
            )
        # Unreachable in practice: existing-agent VM was already
        # cross-checked in the upfront anchor block, and a fresh
        # ephemeral agent was just created on this same VM. Kept as
        # a tripwire so a future refactor that reorders or drops the
        # upfront check fails loudly rather than silently corrupting
        # cross-VM state.
        if agent_row.vm_name != vm.name:
            raise ValidationError(
                f"agent '{agent_name}' is on VM '{agent_row.vm_name}', "
                f"but workspace '{workspace_name}' is on VM '{vm.name}'",
                entity_kind="session",
                entity_name=name,
            )
        linux_user = agent_row.linux_user
        resolved_agent_name = agent_name
        if agent_target is None:
            # The ephemeral agent just realized: probe its direct
            # SSH BEFORE any session mutation, same contract as
            # the existing-agent probe above.
            from agentworks.agents.manager import _assert_agent_ssh_works
            from agentworks.transports import agent_transport

            agent_target = agent_transport(vm, config, agent_row)
            _assert_agent_ssh_works(agent_target, agent_row)
    else:
        mode = SessionMode.ADMIN
        linux_user = vm.admin_username

    # Op-start runup: the required-commands check probes a
    # just-realized ephemeral agent here (it deferred at
    # preflight; the log's flip above is what it observed). For
    # targets that were realized at preflight the check already
    # fired and this is a no-op.
    session_node.runup(
        RunContext(
            config=config,
            operation_scope=scope,
            admin_target=target,
            agent_target=agent_target,
        )
    )

    # Compute socket path up front (deterministic from linux_user +
    # session name). Needed for the DB insert since the CHECK
    # constraint requires agent sessions to have a socket_path.
    expected_socket: str | None = None
    if mode == SessionMode.AGENT:
        from agentworks.sessions.tmux import agent_socket_path

        expected_socket = agent_socket_path(linux_user, name)

    mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
    with output.section("Starting Session"):
        output.info(
            f"Starting session '{name}' on workspace '{workspace_name}' ({mode_label}, template: {template.name})..."
        )

        try:
            # Everything that creates partial session state (on-VM group
            # membership, implicit-grant row, session row, restricted-config
            # write, tmux session) runs inside this block so a KI /
            # exception anywhere here triggers the session node's
            # partial-state teardown.
            if resolved_agent_name is not None:
                # Auto-grant implicit workspace access if the agent has no
                # existing grant on this workspace.
                if not db.has_any_grant(resolved_agent_name, workspace_name):
                    from agentworks.agents.grants import add_to_workspace_group

                    add_to_workspace_group(vm, config, db, linux_user, workspace_name)
                db.insert_agent_grant(resolved_agent_name, workspace_name, "implicit", session_name=name)

            # Op-start RunContext for the harness's start op: mirrors
            # the runup readiness ctx above (targets), plus the scoped
            # secrets (the session node's declared union, empty for the
            # built-in shell harness; ScopedSecrets never delivers).
            # Template-var substitution lifts OUT of the harness and
            # wraps its returned string. The op runs BEFORE the insert
            # so a freshly minted harness_state (claude-code's session
            # id) lands with the new row; it does only read-only work
            # (a login-shell string for shell, a find probe for
            # claude-code), so it stays ahead of any tmux mutation.
            start_ctx = RunContext(
                config=config,
                operation_scope=scope,
                admin_target=target,
                agent_target=agent_target,
                secrets=ScopedSecrets(secret_values, session_node.secret_refs()),
            )
            command = _mgr._substitute_template_vars(
                session_node.harness.start(start_ctx),
                {"session_name": name, "workspace_name": workspace_name},
            )
            if (note := session_node.harness.launch_note()) is not None:
                output.detail(note)

            # Insert DB record before any tmux work so a crash mid-create
            # leaves a recoverable row (and the teardown can find it to
            # delete). The harness's start op ran just above, so its
            # state blob lands with the new row.
            db.insert_session(
                name,
                workspace_name,
                template.name,
                mode,
                agent_name=resolved_agent_name,
                created_workspace=pending_workspace is not None,
                created_agent=pending_agent is not None,
                socket_path=expected_socket,
                harness_state=session_node.harness.state,
            )

            deploy_restricted_config(run_command, history_limit=config.session.history_limit)
            session_env = _mgr._resolve_session_env(
                registry,
                values=secret_values,
                db=db,
                vm=vm,
                ws=ws,
                session_name=name,
                session_template=template,
                mode=mode,
                agent_name=resolved_agent_name,
                linux_user=linux_user,
            )
            # Pick the SSH transport for tmux operations:
            # - admin sessions: admin's run_command (unchanged)
            # - agent sessions: agent's run_command (direct
            #   target-user SSH). agent_target was built and probed above
            #   so a pre-rollout agent never reaches this point. admin's
            #   ``target`` is still passed for socket-root setup which
            #   requires root.
            session_run_command: RunCommand
            if mode == SessionMode.AGENT:
                assert agent_target is not None  # built in the agent branches above
                session_run_command = agent_target.run
            else:
                session_run_command = run_command
            sock, pid = create_tmux_session(
                name,
                ws.workspace_path,
                command,
                linux_user,
                run_command=session_run_command,
                target=target,
                admin_username=vm.admin_username,
                is_admin=(mode == SessionMode.ADMIN),
                env=session_env,
            )
        except (KeyboardInterrupt, Exception):
            # Session-internal cleanup only (DB row, grant, group
            # membership: the node's partial-state teardown). The
            # realized ephemerals are unwound by the outer handlers,
            # whose warn prints one clean reason line before the
            # rollback's delete messages start landing.
            session_node.teardown()
            raise

        # The session's realizing slice is complete: flip the node.
        # Deliberately NOT via the realization log: a completed
        # session (tmux up, row written) is never rolled back, so
        # failures past this point unwind only the ephemerals, and
        # the session survives them. That pins the completed-session
        # window as non-rollbackable.
        session_node.mark_realized()

        # Persist socket path, PID, and boot ID
        if sock:
            db.update_session_socket_path(name, sock)
        if pid is not None:
            boot_id = _mgr._get_boot_id(target)
            if boot_id is not None:
                db.update_session_pid(name, pid, boot_id=boot_id)
            else:
                output.warn(f"Could not read boot ID for session '{name}', PID not stored")
        else:
            output.warn(f"Could not capture PID for session '{name}', will auto-repair on next access")

    # The section is closed: the terminal result line and the
    # post-start bookkeeping (tmuxinator regen, console add) render
    # at column 0, mirroring restart_session. They stay inside the
    # outer try so a failure here still triggers the ephemeral
    # rollback (a completed session itself is never rolled back).
    mode_label = f"agent: {resolved_agent_name}" if resolved_agent_name else "admin"
    output.result(f"Session '{name}' started ({mode_label}, template: {template.name})")

    # Update tmuxinator config and add to console if it exists
    _mgr._regenerate_tmuxinator(db, config, vm, ws)
    from agentworks.sessions.console import add_session_to_console

    add_session_to_console(name, run_command=run_command, socket_path=sock)


def _roll_forward(
    db: Database,
    config: Config,
    registry: Registry,
    plan: SessionPlan,
    graph: SessionGraph,
    vm: VMRow,
    target: Transport,
    run_command: RunCommand,
    agent_target: Transport | None,
    secret_values: dict[str, str],
) -> None:
    """Realize the ephemerals then the session slice, unwinding the
    realized ephemerals on any failure (rollback level 2)."""
    from agentworks.orchestration.unwind import RealizationLog

    # Realize the pending nodes in dependency order, recording each
    # completed realization; on any later failure the log unwinds them in
    # reverse (agent before workspace, today's proven rollback order). The
    # session's own partial state is cleaned by its node's teardown in the
    # slice, and a COMPLETED session (tmux up) is deliberately never
    # rolled back.
    log = RealizationLog()
    try:
        _realize_ephemerals(db, config, registry, plan, graph, vm, secret_values, log)
        _start_session_slice(db, config, registry, plan, graph, vm, target, run_command, agent_target, secret_values)
    except KeyboardInterrupt:
        output.warn(f"Cancelling session create '{plan.name}'... rolling back.")
        log.unwind()
        raise
    except Exception as e:
        # Print the reason BEFORE the rollback's delete-* messages so the
        # operator sees the failure context first, not after a stream of
        # 'Agent deleted' / 'Workspace deleted' lines. The CLI's
        # exception handler still prints the canonical 'Error: ...' line
        # with the typed hint at the very end; this warn just bridges
        # the silence between "thing X created" and the rollback output.
        output.warn(f"Session create '{plan.name}' failed; rolling back. Reason: {e}")
        log.unwind()
        raise
