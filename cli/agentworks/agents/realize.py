"""The phase-free agent realization body.

:func:`realize_agent` is the choreography that makes an agent REAL:
the bespoke mutation an orchestrator runs in its roll-forward, between
the boundary resolve and ``log.mark_realized``. Domain code with
deliberately narrow duties: it frames no phases, resolves no secrets
(``git_tokens`` arrive already resolved, read through the caller's
scoped delivery), opens no gate, and re-checks nothing its caller
already validated (name shape, existence, the VM row). The body owns
the mutation, the git-credential materials ops it carries (whose
write-step runup runs under the skip-and-degrade policy inside
``create_agent_on_vm``), and the mutation's own partial-state cleanup
(a half-configured Linux user), which it unwinds itself before
re-raising. Rollback of a COMPLETED agent is the pending agent node's
``teardown``, driven by the orchestrator's realization log, never this
function's.

This body is what dissolves the ``git_tokens`` + ``own_root`` nesting
hack: the nested ``create_agent`` was a full command root that had to
be handed pre-resolved tokens and phase suppression to stop it
re-running resolve and banners; a body never resolves and never frames
phases, by construction.

Parity oracle: the mutation slice of ``agents.manager.create_agent``,
exactly as ``session create --new-agent`` invoked it nested at the
time this body was factored out (same messages, same error wrapping,
same rollback, minus the nested command root's own registry build,
re-validation, and re-gate). ``grant_all_workspaces`` rides the body
so the grant reconciliation keeps its place between the row insert and
the SSH-config refresh, exactly the imperative order; only the
standalone command offers the flag. Both ``agent create`` and the
session orchestrator call this body; ``agent reinit`` shares the
underlying mutation but not the insert, so it drives
``create_agent_on_vm`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import ExternalError

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, VMRow
    from agentworks.resources.registry import Registry

    from .templates import ResolvedAgentTemplate


def realize_agent(
    db: Database,
    config: Config,
    registry: Registry,
    *,
    name: str,
    vm: VMRow,
    template: ResolvedAgentTemplate,
    git_tokens: dict[str, str],
    grant_all_workspaces: bool = False,
) -> AgentRow:
    """Make agent ``name`` real on ``vm``: create and configure the
    Linux user (including the git-credential materials, their write-step
    runup under the skip-and-degrade policy), insert the DB row, and
    refresh the operator's SSH config.

    Raises on failure AFTER unwinding its own partial state (the
    half-configured user); the caller's realization log never sees a
    half-made agent. Returns the inserted row.
    """
    from agentworks.agents.initializer import create_agent_on_vm, delete_agent_on_vm
    from agentworks.agents.manager import derive_linux_user
    from agentworks.ssh import SSHLogger

    linux_user = derive_linux_user(name)
    ssh_logger = SSHLogger(vm.name, "agent-create")
    # Delivered secret values register on the scope's logger up front
    # (the initialize_vm / reinit_vm pattern): the materials write only
    # ever logs paths and byte counts, so this is defense in depth
    # against any future command or traceback embedding a token.
    for token in git_tokens.values():
        ssh_logger.add_redaction(token)

    def _safe_rollback() -> None:
        # Best-effort: rollback failures must not mask the original KI or
        # exception. Surface them as a warning and let the original error
        # continue to propagate.
        try:
            delete_agent_on_vm(vm, config, linux_user, logger=ssh_logger)
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
            create_agent_on_vm(
                vm,
                config,
                registry,
                template,
                linux_user,
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
        vm.name,
        linux_user,
        template=template.name,
        grant_all=grant_all_workspaces,
    )

    # If grant_all, add to all existing workspace groups
    if grant_all_workspaces:
        from agentworks.agents.grants import add_to_workspace_group

        for ws in db.list_workspaces(vm_name=vm.name):
            add_to_workspace_group(vm, config, db, linux_user, ws.name, logger=None)
            db.insert_agent_grant(name, ws.name, "explicit")

    # Refresh operator SSH config so `ssh <prefix><vm>--<agent>` works.
    # Declarative rebuild from DB state picks up the new agent row.
    from agentworks.ssh_config import sync_ssh_config

    sync_ssh_config(config, db)

    output.info(f"Agent '{name}' created on VM '{vm.name}' (user: {agent.linux_user})")
    return agent
