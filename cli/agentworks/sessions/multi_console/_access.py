"""The accesses one console operation uses on its VM.

``console_access`` is monkeypatched by tests directly on the
``agentworks.sessions.multi_console`` package object, so its callers reach it
through the package object (``_mc.console_access(...)``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import agentworks.sessions.multi_console as _mc
from agentworks.errors import NotFoundError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from agentworks.config import Config
    from agentworks.db import AgentRow, ConsoleSessionRow, Database, VMRow
    from agentworks.execution.access import VMAccess
    from agentworks.vms.initializer.phase_a import OperationLog


@dataclass(frozen=True)
class ConsoleAccess:
    """The accesses one console operation uses on its VM.

    The console's tmux server is the admin's, so every tmux request runs on
    ``admin``. A shell pane parks its request on its own identity's access
    (:meth:`pane`): the admin's for an admin pane, else its agent's, from
    ``agents`` by Linux user. :func:`console_access` builds them and checks
    each agent's direct SSH first, so ``agents`` holds only checked agents.
    """

    vm: VMRow
    config: Config
    admin: VMAccess
    agents: Mapping[str, VMAccess]

    def pane(self, agent_user: str | None) -> VMAccess:
        """The access a shell pane parks on: the admin's for ``None``, else that agent's.

        An agent the operation did not check has no access here, which is a
        bug in the operation, so it raises ``KeyError``.
        """
        return self.admin if agent_user is None else self.agents[agent_user]

    def logged(self, log: OperationLog) -> ConsoleAccess:
        """These accesses with the admin's requests written to ``log``.

        An agent's access only parks, which keeps no log, so it stays as it is.
        """
        import agentworks.execution as execution
        from agentworks.execution.access import Admin

        return replace(self, admin=execution.vm_access(self.vm, self.config, Admin(), log=log.record))


def pane_agent(db: Database, vm: VMRow, session_name: str, *, admin_shell: bool) -> AgentRow | None:
    """The agent a shell pane on ``session_name``'s window runs as, or ``None`` for an admin pane.

    A shell marked ``admin`` and any shell on an admin-mode session run as the
    admin. Raises ``NotFoundError`` for a session that is gone or whose agent
    is gone.
    """
    session = db.get_session(session_name)
    if session is None:
        raise NotFoundError(f"session '{session_name}' not found", entity_kind="session", entity_name=session_name)
    if admin_shell or _mc._session_linux_user(db, session, vm) == vm.admin_username:
        return None
    assert session.agent_name is not None  # _session_linux_user found the agent
    agent = db.get_agent(session.agent_name)
    assert agent is not None
    return agent


def console_access(
    db: Database,
    config: Config,
    vm: VMRow,
    admin: VMAccess,
    members: Iterable[ConsoleSessionRow],
) -> ConsoleAccess:
    """The admin's access and those of the agents whose shell panes ``members`` configure, each checked.

    Each agent's direct SSH is checked once (``_assert_agent_access_works``),
    so call this before the operation's first mutation: an agent that predates
    the direct-SSH rollout is refused with its reinit hint before any pane
    exists. A member whose session or agent is gone has no pane to park for;
    the builders skip it with a warning.
    """
    import agentworks.execution as execution
    from agentworks.agents.manager.access import _assert_agent_access_works
    from agentworks.execution.access import Agent

    agents: dict[str, VMAccess] = {}
    for member in members:
        for shell in member.shells:
            try:
                agent = pane_agent(db, vm, member.session_name, admin_shell=shell["admin"])
            except NotFoundError:
                break
            if agent is None or agent.linux_user in agents:
                continue
            access = execution.vm_access(vm, config, Agent(agent.name, agent.linux_user))
            _assert_agent_access_works(access, admin, agent)
            agents[agent.linux_user] = access
    return ConsoleAccess(vm, config, admin, agents)
