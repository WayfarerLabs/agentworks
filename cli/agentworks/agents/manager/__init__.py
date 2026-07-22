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

``transport`` is re-exported here, not just imported by ``lifecycle``,
because ``tests/conftest.py`` and
``tests/agents/test_delete_grant_revoke_orchestrated.py`` monkeypatch
``agentworks.agents.manager.transport`` directly. ``lifecycle.delete_agent``
reads it back through this package object (``import agentworks.agents.manager
as _mgr`` at module scope, then ``_mgr.transport(...)`` at call time)
rather than importing ``transport`` by value, so the monkeypatch on this
module's attribute still reaches that call. Likewise ``delete_agent``
itself is patched by the session-create ephemeral-rollback tests;
external callers (``agentworks.sessions.manager``, ``agents/nodes.py``) import
it lazily from this package (``from agentworks.agents.manager import
delete_agent``) so the patch reaches them too.
"""

from __future__ import annotations

from agentworks.transports import transport

from ._common import (
    AGENT_PREFIX,
    MAX_GRANTS_DISPLAY,
    _agent_direct_secret_target,
    _AgentDirectEnvScopes,
    _assert_agent_ssh_works,
    _require_vm,
    _require_vm_for_workspace,
    _require_workspace,
    _resolve_agent_direct_env_scopes,
    _resolve_workspace_for_agent,
    agent_scope,
    derive_linux_user,
)
from .access import exec_agent, shell_agent
from .inspect import _format_grants, describe_agent, list_agents
from .lifecycle import create_agent, delete_agent, reinit_agent

__all__ = [
    "AGENT_PREFIX",
    "MAX_GRANTS_DISPLAY",
    "_AgentDirectEnvScopes",
    "_agent_direct_secret_target",
    "_assert_agent_ssh_works",
    "_format_grants",
    "_require_vm",
    "_require_vm_for_workspace",
    "_require_workspace",
    "_resolve_agent_direct_env_scopes",
    "_resolve_workspace_for_agent",
    "agent_scope",
    "create_agent",
    "delete_agent",
    "derive_linux_user",
    "describe_agent",
    "exec_agent",
    "list_agents",
    "reinit_agent",
    "shell_agent",
    "transport",
]
