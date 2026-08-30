"""The phase-free agent realization body.

:func:`realize_agent` is the choreography that makes an agent REAL:
the bespoke mutation an orchestrator runs in its roll-forward, between
the boundary resolve and ``log.mark_realized``. Domain code with
deliberately narrow duties: it frames no phases, resolves no secrets
(credential requests arrive with their scoped contexts already prepared),
opens no gate, and re-checks nothing its caller
already validated (name shape, existence, the VM row). The body owns
the mutation, the git-credential materials ops it carries (whose
write-step runup runs under the skip-and-degrade policy inside
``create_agent_on_vm``), and the mutation's own partial-state cleanup
(a half-configured Linux user), which it unwinds itself before
re-raising. Rollback of a COMPLETED agent is the pending agent node's
``teardown``, driven by the orchestrator's realization log, never this
function's.

This body is what dissolves the old resolved-credential + ``own_root`` nesting
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
    from pydantic import BaseModel

    from agentworks.config import Config
    from agentworks.db import AgentRow, Database, VMRow
    from agentworks.git_credentials import CredentialRequest
    from agentworks.instance_specs import InstanceOverlay
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
    credential_requests: tuple[CredentialRequest, ...],
    credential_redactions: tuple[str, ...],
    grant_all_workspaces: bool = False,
    overlay: InstanceOverlay[BaseModel] | None = None,
    defer_overlay_report: bool = False,
) -> AgentRow:
    """Make agent ``name`` real on ``vm``: create and configure the
    Linux user (including the git-credential materials, their write-step
    runup under the skip-and-degrade policy), insert the DB row, and
    refresh the operator's SSH config.

    Raises on failure AFTER unwinding its own partial state (the
    half-configured user); the caller's realization log never sees a
    half-made agent. Returns the inserted row.
    """
    # DRIFT GUARD (Phase 7, recipe use-gate): this choreography is itself never
    # gated; the recipe gate (``ensure_recipe_enabled``) lives at each COMMAND
    # ENTRY that reaches it (agent create, session create --new-agent). If you
    # add a NEW caller of ``realize_agent``, add its command-entry gate and
    # update tests/agents/test_recipe_gate_drift.py's enumerated caller set.
    from agentworks.agents.initializer import create_agent_on_vm, delete_agent_on_vm
    from agentworks.agents.manager import derive_linux_user
    from agentworks.ssh import SSHLogger

    linux_user = derive_linux_user(name)
    # Supply every declared secret input when the logger is constructed. A logger's
    # redaction set is immutable because adding a secret after the first
    # incremental write cannot protect bytes already persisted.
    ssh_logger = SSHLogger(vm.name, "agent-create", redactions=credential_redactions)

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
                f"SSH log: {ssh_logger.display_path}"
            )

    # The logger's close() writes a "Finished" footer; defer it via finally so
    # rollback commands are logged BEFORE the footer, not after.
    try:
        try:
            from agentworks.vms.admin_templates import resolve_live_template as resolve_admin_template

            create_agent_on_vm(
                vm,
                config,
                registry,
                template,
                linux_user,
                agent_name=name,
                credential_requests=credential_requests,
                logger=ssh_logger,
                admin_git_force_safe_directory=resolve_admin_template(
                    db,
                    registry,
                    vm.name,
                    vm.admin_template,
                ).git_force_safe_directory,
            )

            from agentworks.instance_specs import persist_creation_overlay, refuse_orphan_creation_state

            with db.transaction():
                refuse_orphan_creation_state(db, "agent", name)
                agent = db.insert_agent(
                    name,
                    vm.name,
                    linux_user,
                    template=template.name,
                    grant_all=grant_all_workspaces,
                )
                overlay_outcome = persist_creation_overlay(db, "agent", name, overlay)
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
                hint=f"SSH log: {ssh_logger.display_path}",
            ) from e
    finally:
        try:
            ssh_logger.close()
        except BaseException:
            if not defer_overlay_report:
                from agentworks.instance_specs import render_retained_creation_overlay

                render_retained_creation_overlay(db, "agent", name)
            raise

    from agentworks.instance_specs import report_overlay_outcome

    with report_overlay_outcome(None if defer_overlay_report else overlay_outcome):
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
