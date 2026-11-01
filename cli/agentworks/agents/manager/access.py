"""Agent shell / exec: the direct-SSH surface.

Direct-agent-user SSH, as opposed to the admin+sudo detour: the
agent's own ``authorized_keys`` accepts the operator's key (see
``agents/initializer.py``). Both commands reach the VM through
``agentworks.execution``, and this file imports no retired transport module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentworks.agents.manager as _mgr
import agentworks.execution as execution
from agentworks.errors import ConnectivityError, NotFoundError, StateError
from agentworks.execution.access import Admin, Agent, stream_to_operator
from agentworks.execution.requests import Command, LoginShell, Script, Shell
from agentworks.execution.results import Completed, Unknown, UnknownReason
from agentworks.vms.manager import gated_vm_boundary

from ._common import (
    _require_vm,
    _resolve_workspace_for_agent,
    agent_scope,
)

# ``_resolve_agent_direct_env_scopes`` and ``_agent_direct_secret_target`` are
# referenced through the package object (``_mgr.NAME``) at call time rather
# than imported by value, because tests monkeypatch them on
# ``agentworks.agents.manager``; a by-value import here would freeze a copy
# the patch could not reach.

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import AgentRow, Database
    from agentworks.execution.access import VMAccess
    from agentworks.secrets.policy import TtyInteractionPolicy

_PROBE_DEADLINE_SECONDS = 30
_ADMIN_PROBE_DEADLINE_SECONDS = 10


def _assert_agent_access_works(access: VMAccess, admin: VMAccess, agent: AgentRow) -> None:
    """Probe direct agent SSH and tell an agent that predates the rollout from an unreachable VM.

    The direct-target-user-SSH rollout populates each agent's
    ``~/.ssh/authorized_keys`` with the operator's key set at agent create /
    reinit. An agent that existed before it has no such file, so ssh as the
    agent is rejected (255). Run ``true`` as the agent; a ``Completed`` of any
    status passes. Only an unknown connection failure is ambiguous, so only it
    falls back to running ``true`` as the admin: if that completes, the VM is
    fine and the agent is the problem (the reinit hint); otherwise the VM is
    unreachable. Every other outcome (python3 missing, a host key mismatch, a
    refusal, a truncated run) is not about the rollout and raises as it is.
    """
    result = access.run(Command(("true",), deadline=_PROBE_DEADLINE_SECONDS))
    completion = result.completion
    if isinstance(completion, Completed):
        return
    if not (isinstance(completion, Unknown) and completion.reason is UnknownReason.CONNECTION):
        result.require_completed(access.vm_name)
        return
    fallback = admin.run(Command(("true",), deadline=_ADMIN_PROBE_DEADLINE_SECONDS))
    if isinstance(fallback.completion, Completed):
        raise StateError(
            f"agent '{agent.name}' rejected direct SSH (likely predates the direct-target-user-SSH rollout).",
            entity_kind="agent",
            entity_name=agent.name,
            hint=f"Run 'agw agent reinit {agent.name}' to populate its authorized_keys.",
        )
    raise ConnectivityError(
        f"direct SSH probe to agent '{agent.name}' failed: VM '{agent.vm_name}' is unreachable",
        entity_kind="agent",
        entity_name=agent.name,
        hint=f"Check that VM '{agent.vm_name}' is reachable.",
    )


def shell_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str | None = None,
    interaction: TtyInteractionPolicy,
) -> int:
    """Open a shell as an agent user on a VM.

    Returns the interactive session's exit code; the CLI layer owns the
    translation to process exit (check 9: no sys.exit in the service),
    mirroring :func:`agentworks.vms.manager.exec_vm`.

    Orchestrated (``vms.manager.gated_vm_boundary``): the graph
    derives from the VM's row, the activation gate replaces this
    command's ``keep_active`` use (opening BEFORE the preflight sweep;
    its just-in-time values seed the boundary resolver), and the
    held-active span covers the whole interactive session.
    """
    # Before any database read or guard: an unsupported client refuses first.
    execution.resolve_ssh()
    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    vm = _require_vm(db, agent.vm_name)

    from agentworks.env import ResourceContext, compose_env

    # Resolve workspace upfront (needed for authz check, env scope, AND
    # ctx) before any SSH probe so failures surface as clean validation
    # errors and the eager-resolve below sees the right scope chain.
    ws = _resolve_workspace_for_agent(db, vm, agent, workspace_name)

    # The orchestrated composition root (gated_vm_boundary): the agent
    # shell's env-chain secrets join the ONE boundary resolve (site
    # secrets + env secrets, one prompt session), after every node's
    # preflight; the activation gate opens before the sweep and its
    # held-active span covers the whole interactive session. The same
    # scope dicts feed both the SecretTarget and compose_env below so
    # the two consumers can't drift.
    from agentworks.bootstrap import load_request_registry

    registry = load_request_registry(config, live_database=db)
    scopes = _mgr._resolve_agent_direct_env_scopes(db, registry, vm, agent, ws=ws)

    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        targets=[_mgr._agent_direct_secret_target(scopes, label=f"agent-shell={agent.name}")],
        scope=agent_scope(db, vm.name, agent.name),
        interaction=interaction,
    ) as (_vm_node, resolver, _ops_ctx):
        from agentworks.vms.sites import site_platform_name

        ctx = ResourceContext(
            vm_name=vm.name,
            platform=site_platform_name(vm.site, registry),
            site=vm.site,
            user=agent.linux_user,
            workspace_name=ws.name if ws else None,
            workspace_dir=ws.workspace_path if ws else None,
            agent_name=agent.name,
        )
        env = compose_env(
            values=resolver.values,
            ctx=ctx,
            vm=scopes.vm,
            workspace=scopes.workspace,
            agent=scopes.agent,
        )

        # Direct agent SSH: no admin+sudo detour. The agent's
        # authorized_keys accepts the operator's key set.
        access = execution.vm_access(vm, config, Agent(agent.name, agent.linux_user))

        # Probe direct agent SSH first so pre-rollout agents (whose
        # authorized_keys was never populated) get an actionable error
        # rather than dropping into a remote shell that immediately exits
        # on Permission denied.
        _assert_agent_access_works(access, execution.vm_access(vm, config, Admin()), agent)

        # An interactive login shell as the agent, in the workspace when one
        # is named. No sudo / su involved.
        return access.attach(LoginShell(cwd=ws.workspace_path if ws else None, env=env))


def exec_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    command: list[str],
    workspace_name: str | None = None,
    interaction: TtyInteractionPolicy,
) -> int:
    """Execute a command as an agent user on a VM via direct agent SSH.

    Opens a non-interactive SSH session directly as the agent's Linux user
    and runs the command in a login shell so the agent's PATH /
    profile is in scope. Stdout / stderr stream through to the caller; the
    return value is the remote command's exit code.

    When ``workspace_name`` is set, the command runs from the workspace
    directory and the workspace template's env joins the env chain. The
    workspace must belong to the agent's VM and the agent must have
    access.

    Orchestrated (``vms.manager.gated_vm_boundary``), mirroring
    :func:`shell_agent`: the gate opens before the preflight sweep and
    the held-active span covers the streamed remote command.
    """
    import shlex

    from agentworks.env import ResourceContext, compose_env
    from agentworks.exec_validation import normalize_exec_command

    # Before any database read or guard: an unsupported client refuses first.
    execution.resolve_ssh()
    command = normalize_exec_command(command, kind="agent", name=name)

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

    # The orchestrated composition root (gated_vm_boundary): the agent
    # exec env-chain secrets join the ONE boundary resolve (site
    # secrets + env secrets, one prompt session), after every node's
    # preflight; the gate's held-active span covers the streamed
    # remote command. The same scope dicts feed both the SecretTarget
    # and compose_env below so the two consumers can't drift.
    from agentworks.bootstrap import load_request_registry

    registry = load_request_registry(config, live_database=db)
    scopes = _mgr._resolve_agent_direct_env_scopes(db, registry, vm, agent, ws=ws)

    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        targets=[_mgr._agent_direct_secret_target(scopes, label=f"agent-exec={agent.name}")],
        scope=agent_scope(db, vm.name, agent.name),
        interaction=interaction,
    ) as (_vm_node, resolver, _ops_ctx):
        from agentworks.vms.sites import site_platform_name

        ctx = ResourceContext(
            vm_name=vm.name,
            platform=site_platform_name(vm.site, registry),
            site=vm.site,
            user=agent.linux_user,
            workspace_name=ws.name if ws else None,
            workspace_dir=ws.workspace_path if ws else None,
            agent_name=agent.name,
        )
        env = compose_env(
            values=resolver.values,
            ctx=ctx,
            vm=scopes.vm,
            workspace=scopes.workspace,
            agent=scopes.agent,
        )

        access = execution.vm_access(vm, config, Agent(agent.name, agent.linux_user))

        # Probe direct agent SSH first so pre-rollout agents (whose
        # authorized_keys was never populated) get an actionable error.
        _assert_agent_access_works(access, execution.vm_access(vm, config, Admin()), agent)

        # A login shell runs the command so the agent's PATH (mise shims,
        # ~/.local/bin, etc.) is set up. This matches the env an operator
        # gets via `agent shell`. One argument is a command line; several are
        # joined into one, as `$SHELL -lc` has always taken them.
        source = command[0] if len(command) == 1 else shlex.join(command)
        request = Script(source, Shell.USER_DEFAULT, login=True, cwd=ws.workspace_path if ws else None, env=env)
        return stream_to_operator(access, request)
