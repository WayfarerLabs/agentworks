"""Shared constants, scopes, and row-lookup guards for the agents manager.

Backing module for the ``agentworks.agents.manager`` package: the small
cross-cutting pieces ``lifecycle``, ``inspect``, and ``access`` all
depend on (the ``AGENT_PREFIX`` / ``MAX_GRANTS_DISPLAY`` constants, the
AGENT-level operation scope, the direct-shell/exec env-scope
resolution, and the ``_require_*`` row-lookup guards). Nothing here is
orchestrated; it is plain data shaping and DB lookups shared across the
package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from agentworks.errors import (
    AuthorizationError,
    NotFoundError,
    StateError,
    ValidationError,
)

# Importing the Linux username ceiling from the config layer (never the
# reverse) keeps the dependency direction safe: config.validation depends on
# nothing in the agents package, so this cannot cycle. It backs the derived
# MAX_AGENT_NAME_LENGTH co-located with AGENT_PREFIX below.
from agentworks.naming import LINUX_USERNAME_MAX_LENGTH

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope
    from agentworks.db import AgentRow, Database, VMRow, WorkspaceRow
    from agentworks.env import EnvEntry
    from agentworks.resources import Registry
    from agentworks.secrets import SecretTarget
    from agentworks.transports import Transport

AGENT_PREFIX = "agt-"

# Derived FROM the prefix so a prefix change cannot reintroduce an over-limit
# Linux username. ``derive_linux_user`` builds ``agt-<name>`` and ``useradd -U``
# creates a same-named group, so the derived string must fit the 32-char Linux
# username/group limit: 32 - len("agt-") = 28.
MAX_AGENT_NAME_LENGTH = LINUX_USERNAME_MAX_LENGTH - len(AGENT_PREFIX)

MAX_GRANTS_DISPLAY = 60


def agent_scope(db: Database, vm_name: str, agent_name: str) -> OperationScope:
    """The agent commands' shared AGENT-level operation scope (public:
    ``agents.grants`` shares it): the operation is about the agent (on
    its VM), even though the composed graph is the live VM alone; pass
    the level of the entity the command is ABOUT, not of what it
    walks. The AGENT level's field rules (required vm + agent;
    forbidden workspace, session: agents are VM-scoped, a workspace
    relationship is a grant, never identity) are enforced by the
    scope's own constructor."""
    from agentworks.capabilities.base import OperationScope, ScopeLevel
    from agentworks.db import SYSTEM_SLUG_KEY

    return OperationScope(
        level=ScopeLevel.AGENT,
        system_slug=db.get_setting(SYSTEM_SLUG_KEY) or None,
        vm=vm_name,
        agent=agent_name,
    )


def agent_has_sessions(db: Database, name: str) -> bool:
    """Whether any session still runs under this agent.

    ``list_sessions(agent_name=...)`` matches the agent_name column directly
    and excludes admin sessions, which is exactly the set that keeps an agent
    alive. It reads live DB state, so a caller that wants the count AFTER a
    session row is removed must call it once the delete has committed.
    """
    return bool(db.list_sessions(agent_name=name))


def agent_has_grants(db: Database, name: str) -> bool:
    """Whether the agent holds any standing workspace grant.

    Standing grant intent takes two forms, both of which count:

    - The persisted ``grant_all`` flag on the agent row ("may access every
      workspace"). This is the strongest form and can hold with ZERO
      explicit rows (``grant_all`` only materializes explicit rows for
      workspaces that exist at grant time, and those rows cascade away when
      a workspace is deleted), so it must be read directly from the agent
      row, never inferred from the grant table.
    - An explicit grant row in ``agent_workspace_grants``.

    Implicit grants are tied to individual sessions (removed as those
    sessions are deleted), so they deliberately do not count here.
    """
    agent = db.get_agent(name)
    if agent is not None and agent.grant_all:
        return True
    return any(has_explicit for (_ws, has_explicit, _implicit) in db.list_granted_workspaces_with_types(name))


def agent_is_unused(db: Database, name: str) -> bool:
    """Whether the agent has neither sessions nor standing workspace grants.

    This public predicate composes :func:`agent_has_sessions` and
    :func:`agent_has_grants` into the complete live-state definition of an
    unused agent.
    """
    return not agent_has_sessions(db, name) and not agent_has_grants(db, name)


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
    scope precedence ladder between vm and agent.
    """

    vm: dict[str, EnvEntry]
    workspace: dict[str, EnvEntry] | None
    agent: dict[str, EnvEntry]


def _resolve_agent_direct_env_scopes(
    db: Database,
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

    Scope sources mirror the scope precedence ladder:

    - ``vm``: the VM's actual template env (from the ``vm.template`` DB row).
    - ``workspace``: when ``ws`` is supplied, the workspace template's env.
    - ``agent``: the agent row's template env (from the DB row).
      The agent pre-exists this call and may have been created under a
      different template than the operator's current ``--template``
      would resolve.
    """
    from agentworks.agents.templates import resolve_live_template as _resolve_agent_template
    from agentworks.vms.templates import resolve_live_template as _resolve_vm_template
    from agentworks.workspaces.templates import resolve_live_template as _resolve_ws_template

    vm_tmpl = _resolve_vm_template(db, registry, vm.name, vm.template)
    agent_tmpl = _resolve_agent_template(db, registry, agent.name, agent.template)
    ws_env: dict[str, EnvEntry] | None = None
    if ws is not None:
        ws_env = _resolve_ws_template(db, registry, ws.name, ws.template).env
    return _AgentDirectEnvScopes(
        vm=vm_tmpl.env,
        workspace=ws_env,
        agent=agent_tmpl.env,
    )


def _agent_direct_secret_target(
    scopes: _AgentDirectEnvScopes,
    *,
    label: str,
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
    db: Database,
    vm: VMRow,
    agent: AgentRow,
    workspace_name: str | None,
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
            f"agent '{agent.name}' rejected direct SSH (likely predates the direct-target-user-SSH rollout).",
            entity_kind="agent",
            entity_name=agent.name,
            hint=f"Run 'agw agent reinit {agent.name}' to populate its authorized_keys.",
        )


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
