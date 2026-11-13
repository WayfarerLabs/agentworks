"""Agent lifecycle orchestration.

The command layer of the agents domain: create / reinit / delete,
list / describe, and the direct shell / exec surface. The on-VM
provisioning bodies live in ``agents/initializer.py``; the
workspace-grant commands and group-membership primitives live in
``agents/grants.py``; the realization body shared with the session
orchestrator lives in ``agents/realize.py``.

Split into a package so each submodule stays under the file-size
convention: ``_common`` (constants, scopes, and the ``_require_*`` /
env-scope helpers shared by the rest), ``lifecycle`` (create / delete /
reinit), ``inspect`` (list / describe), and ``access`` (shell / exec).
This module re-exports the public surface plus the handful of names
tests reach directly or via monkeypatch, so ``agentworks.agents.manager``
stays the one import path every caller (and every test) uses.

``delete_agent`` is patched by the session-create ephemeral-rollback tests;
external callers (``agentworks.sessions.manager``, ``agents/nodes.py``) import
it lazily from this package (``from agentworks.agents.manager import
delete_agent``) so the patch reaches them too.
"""

from __future__ import annotations

from ._common import (
    AGENT_PREFIX,
    MAX_AGENT_NAME_LENGTH,
    MAX_GRANTS_DISPLAY,
    _agent_direct_secret_target,
    _AgentDirectEnvScopes,
    _require_vm,
    _require_vm_for_workspace,
    _require_workspace,
    _resolve_agent_direct_env_scopes,
    _resolve_workspace_for_agent,
    agent_has_grants,
    agent_has_sessions,
    agent_is_unused,
    agent_scope,
    derive_linux_user,
)
from .access import exec_agent, shell_agent
from .inspect import (
    agent_description,
    agent_listing,
    describe_agent,
    list_agents,
    render_agent_description,
    render_agent_listing,
)
from .lifecycle import create_agent, delete_agent, reinit_agent

__all__ = [
    "AGENT_PREFIX",
    "MAX_AGENT_NAME_LENGTH",
    "MAX_GRANTS_DISPLAY",
    "_AgentDirectEnvScopes",
    "_agent_direct_secret_target",
    "_require_vm",
    "_require_vm_for_workspace",
    "_require_workspace",
    "_resolve_agent_direct_env_scopes",
    "_resolve_workspace_for_agent",
    "agent_description",
    "agent_has_grants",
    "agent_has_sessions",
    "agent_is_unused",
    "agent_scope",
    "agent_listing",
    "create_agent",
    "delete_agent",
    "derive_linux_user",
    "describe_agent",
    "exec_agent",
    "list_agents",
    "reinit_agent",
    "render_agent_description",
    "render_agent_listing",
    "shell_agent",
]
