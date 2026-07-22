"""Agent shell / exec: the direct-SSH surface.

Direct-agent-user SSH, as opposed to the admin+sudo detour: the
agent's own ``authorized_keys`` accepts the operator's key (see
``agents/initializer.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import NotFoundError
from agentworks.vms.manager import gated_vm_boundary

from ._common import (
    _agent_direct_secret_target,
    _assert_agent_ssh_works,
    _require_vm,
    _resolve_agent_direct_env_scopes,
    _resolve_workspace_for_agent,
    agent_scope,
)

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database


def shell_agent(
    db: Database,
    config: Config,
    *,
    name: str,
    workspace_name: str | None = None,
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
    agent = db.get_agent(name)
    if agent is None:
        raise NotFoundError(
            f"agent '{name}' not found",
            entity_kind="agent",
            entity_name=name,
        )

    vm = _require_vm(db, agent.vm_name)

    from agentworks.env import ResourceContext, compose_env
    from agentworks.transports import agent_transport

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
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    scopes = _resolve_agent_direct_env_scopes(registry, vm, agent, ws=ws)

    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        targets=[_agent_direct_secret_target(scopes, label=f"agent-shell={agent.name}")],
        scope=agent_scope(db, vm.name, agent.name),
    ) as (_vm_node, resolver):
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
        target = agent_transport(vm, config, agent)

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
            return target.interactive(shell_cmd, env=env)
        # SSH as the agent with no command -> interactive login shell.
        return target.interactive("", env=env)


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
    from agentworks.exec_validation import reject_dash_prefixed_command
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

    # The orchestrated composition root (gated_vm_boundary): the agent
    # exec env-chain secrets join the ONE boundary resolve (site
    # secrets + env secrets, one prompt session), after every node's
    # preflight; the gate's held-active span covers the streamed
    # remote command. The same scope dicts feed both the SecretTarget
    # and compose_env below so the two consumers can't drift.
    from agentworks.bootstrap import build_registry

    registry = build_registry(config)
    scopes = _resolve_agent_direct_env_scopes(registry, vm, agent, ws=ws)

    with gated_vm_boundary(
        db,
        config,
        registry,
        vm,
        targets=[_agent_direct_secret_target(scopes, label=f"agent-exec={agent.name}")],
        scope=agent_scope(db, vm.name, agent.name),
    ) as (_vm_node, resolver):
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

        target = agent_transport(vm, config, agent)

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
            f"$SHELL -lc {shlex.quote(remote_cmd)}",
            env=env,
        )
