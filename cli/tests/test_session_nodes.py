"""The session / agent / workspace nodes: the four-way readiness fork,
the one-object-per-node target contract, the derived multi-consumer
graph, and the relocated ephemeral teardown bodies.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel
from agentworks.errors import StateError
from agentworks.sessions.nodes import pending_session_node
from agentworks.sessions.templates import ResolvedSessionTemplate
from agentworks.vms.nodes import LiveVMNode, VMSiteNode

if TYPE_CHECKING:
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.resources.registry import Registry


class _Platform:
    name = "stub"


class _Probe:
    """Recording transport double for the required-commands probe."""

    def __init__(self, missing: set[str] | None = None) -> None:
        self._missing = missing or set()
        self.commands: list[str] = []

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(cmd)
        ok = not any(f"command -v {m} " in cmd for m in self._missing)
        return SimpleNamespace(ok=ok)


def _vm_node(db: Database, name: str = "box") -> LiveVMNode:
    db.insert_vm(name, site="stub", hostname=name)
    row = db.get_vm(name)
    assert row is not None
    site = VMSiteNode("stub", cast("VMPlatform", _Platform()), ())
    return LiveVMNode(
        db, cast("Config", object()), cast("Registry", object()), row, site
    )


def _pending_agent(db: Database, vm: LiveVMNode, name: str = "dev"):
    from agentworks.agents.nodes import AgentTemplateNode, pending_agent_node
    from agentworks.agents.templates import ResolvedAgentTemplate

    template = AgentTemplateNode(ResolvedAgentTemplate(name="default"), ())
    return pending_agent_node(db, cast("Config", object()), name, template, vm)


def _session(
    db: Database,
    *,
    required: tuple[str, ...] = ("claude",),
    agent: object | None = None,
    admin: bool = False,
    vm: LiveVMNode | None = None,
):
    from agentworks.workspaces.nodes import pending_workspace_node

    vm_node = vm if vm is not None else _vm_node(db)
    workspace = pending_workspace_node(
        db, cast("Config", object()), "ws1", vm_node, None
    )
    template = ResolvedSessionTemplate(
        name="claude", required_commands=list(required)
    )
    return pending_session_node(
        "s1",
        template,
        agent=agent,  # type: ignore[arg-type]
        admin=admin,
        workspace=workspace,
        vm=vm_node,
    )


def _ctx(
    level: ScopeLevel = ScopeLevel.SESSION,
    *,
    agent_target: object | None = None,
    admin_target: object | None = None,
    agent: str | None = "dev",
    admin: bool = False,
) -> RunContext:
    if level is ScopeLevel.SESSION:
        scope = OperationScope(
            level=level, vm="box", workspace="ws1", session="s1",
            agent=agent, admin=admin,
        )
    else:
        scope = OperationScope(level=level)
    return RunContext(
        operation_scope=scope,
        agent_target=agent_target,  # type: ignore[arg-type]
        admin_target=admin_target,  # type: ignore[arg-type]
    )


# -- the four-way readiness fork ---------------------------------------------


def test_system_level_scan_skips_the_check(db: Database) -> None:
    """The level-driven SKIP branch's first real exercise: a
    system-scoped doctor scan reaching a session no-ops the
    required-commands check, even with no target at all, rather than
    erroring: out of scope for the level is legitimate, not a bug."""
    session = _session(db, agent=None, admin=True)
    session.preflight(_ctx(ScopeLevel.SYSTEM))  # no probe, no raise
    session.runup(_ctx(ScopeLevel.SYSTEM))  # still nothing


def test_pending_target_defers_then_probes_after_the_flip(db: Database) -> None:
    """Defer-then-probe, and the one-object contract in action: the
    check watches the SAME agent object the log flips, so realization
    is observed without rewiring."""
    vm = _vm_node(db)
    agent = _pending_agent(db, vm)
    session = _session(db, agent=agent, vm=vm)
    probe = _Probe()

    session.preflight(_ctx(agent_target=probe))
    assert probe.commands == []  # pending target: deferred

    agent.mark_realized()  # the orchestrator's flip, same object
    session.runup(_ctx(agent_target=probe))
    assert len(probe.commands) == 1  # probed exactly once, post-flip


def test_realized_target_probes_at_preflight(db: Database) -> None:
    """The earlier-failure win for existing agents: in scope and
    realized probes NOW, pre-resolve, and fires only once."""
    vm = _vm_node(db)
    agent = _pending_agent(db, vm)
    agent.mark_realized()
    session = _session(db, agent=agent, vm=vm)
    probe = _Probe()

    session.preflight(_ctx(agent_target=probe))
    assert len(probe.commands) == 1
    session.runup(_ctx(agent_target=probe))
    assert len(probe.commands) == 1  # fired once


def test_missing_command_is_a_typed_error(db: Database) -> None:
    vm = _vm_node(db)
    agent = _pending_agent(db, vm)
    agent.mark_realized()
    session = _session(db, required=("claude", "rg"), agent=agent, vm=vm)
    probe = _Probe(missing={"rg"})

    with pytest.raises(StateError, match="requires 'rg'") as exc:
        session.preflight(_ctx(agent_target=probe))
    assert "agent 'dev'" in str(exc.value)
    assert "--template" in (exc.value.hint or "")


def test_absent_target_is_a_loud_error(db: Database) -> None:
    """Anti-silent-skip: in scope with no target is a selection bug."""
    from agentworks.sessions.nodes import RequiredCommandsCheck

    check = RequiredCommandsCheck(
        session_name="s1",
        template_name="claude",
        required_commands=("claude",),
        target=None,
        admin=False,
    )
    with pytest.raises(StateError, match="refusing to skip"):
        check.preflight(_ctx())


def test_admin_mode_probes_the_admin_target(db: Database) -> None:
    session = _session(db, agent=None, admin=True)
    probe = _Probe()
    session.preflight(_ctx(agent=None, admin=True, admin_target=probe))
    assert len(probe.commands) == 1


def test_missing_transport_defers_at_preflight_and_is_loud_at_runup(
    db: Database,
) -> None:
    """A realized target with no transport on the command-start context
    defers (the stage's timing did not carry it); the op-start context
    must carry it, so runup without one is a loud error."""
    vm = _vm_node(db)
    agent = _pending_agent(db, vm)
    agent.mark_realized()
    session = _session(db, agent=agent, vm=vm)

    session.preflight(_ctx())  # no transport: defer, no raise
    with pytest.raises(StateError, match="op-start context"):
        session.runup(_ctx())


# -- the factory contracts ---------------------------------------------------


def test_session_factory_requires_exactly_one_launch_identity(
    db: Database,
) -> None:
    vm = _vm_node(db)
    with pytest.raises(StateError, match="exactly one"):
        _session(db, agent=None, admin=False, vm=vm)
    agent = _pending_agent(db, vm)
    with pytest.raises(StateError, match="exactly one"):
        _session(db, agent=agent, admin=True, vm=vm)


def test_session_deps_carry_the_same_agent_object(db: Database) -> None:
    vm = _vm_node(db)
    agent = _pending_agent(db, vm)
    session = _session(db, agent=agent, vm=vm)
    assert session.deps()[0] is agent


# -- the derived multi-consumer graph ----------------------------------------


def test_session_create_graph_shares_one_vm_node(db: Database) -> None:
    """The first true diamond: workspace, agent, and session all reach
    the SAME VM node object; the walk visits it once and yields
    dependencies before dependents. The agent's git-credential edges
    enter through its template (the graph replaces the hand-rolled
    ephemeral fold)."""
    from agentworks.agents.nodes import AgentTemplateNode, pending_agent_node
    from agentworks.agents.templates import ResolvedAgentTemplate
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import GitCredentialNode
    from agentworks.workspaces.nodes import pending_workspace_node

    vm = _vm_node(db)
    provider = SimpleNamespace(
        owner_name="gh", secret_name="git-token-gh",
        preflight=lambda ctx: None, runup=lambda ctx: None,
    )
    cred = GitCredentialNode("gh", provider, ("git-token-gh",))  # type: ignore[arg-type]
    template = AgentTemplateNode(
        ResolvedAgentTemplate(name="default", git_credentials=["gh"]), (cred,)
    )
    agent = pending_agent_node(db, cast("Config", object()), "dev", template, vm)
    workspace = pending_workspace_node(
        db, cast("Config", object()), "ws1", vm, None
    )
    session = pending_session_node(
        "s1",
        ResolvedSessionTemplate(name="claude"),
        agent=agent,
        admin=False,
        workspace=workspace,
        vm=vm,
    )

    nodes = walk(session)
    assert [n.key for n in nodes] == [
        "git-credential/gh",
        "agent-template/default",
        "vm-site/stub",
        "vm/box",
        "agent/dev",
        "workspace/ws1",
        "session/s1",
    ]
    from agentworks.orchestration.secrets import secret_union

    assert secret_union(nodes) == ("git-token-gh",)


# -- the relocated ephemeral teardown bodies ---------------------------------


def test_pending_agent_teardown_is_todays_rollback_body(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.agents import manager as agents_manager

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        agents_manager,
        "delete_agent",
        lambda db_, config, **kw: calls.append(dict(kw)),
    )
    vm = _vm_node(db)
    agent = _pending_agent(db, vm)
    agent.mark_realized()
    agent.teardown()
    (call,) = calls
    assert call["name"] == "dev"
    assert call["force"] is True and call["yes"] is True
    assert call["platform"] is vm.site.platform


def test_pending_agent_teardown_failure_names_the_artifact(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.agents import manager as agents_manager

    def _boom(*a: object, **k: object) -> None:
        raise RuntimeError("ssh down")

    monkeypatch.setattr(agents_manager, "delete_agent", _boom)
    vm = _vm_node(db)
    agent = _pending_agent(db, vm)
    with pytest.raises(StateError) as exc:
        agent.teardown()
    assert "ephemeral agent 'dev'" in str(exc.value)
    assert "left standing" in str(exc.value)
    assert "agw agent delete --force dev" in str(exc.value)


def test_pending_workspace_teardown_is_todays_rollback_body(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.workspaces import manager as workspaces_manager
    from agentworks.workspaces.nodes import pending_workspace_node

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        workspaces_manager,
        "delete_workspace",
        lambda db_, config, **kw: calls.append(dict(kw)),
    )
    vm = _vm_node(db)
    workspace = pending_workspace_node(
        db, cast("Config", object()), "ws1", vm, None
    )
    workspace.mark_realized()
    workspace.teardown()
    (call,) = calls
    assert call["name"] == "ws1"
    assert call["force"] is True and call["yes"] is True


def test_reverse_realization_order_reproduces_rollback_order(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unwind oracle for the ephemeral fold: agent BEFORE workspace
    (reverse of workspace-then-agent creation), so the agent's
    workspace-group membership is cleaned before the group goes away."""
    from agentworks.agents import manager as agents_manager
    from agentworks.orchestration.unwind import RealizationLog
    from agentworks.workspaces import manager as workspaces_manager
    from agentworks.workspaces.nodes import pending_workspace_node

    order: list[str] = []
    monkeypatch.setattr(
        agents_manager,
        "delete_agent",
        lambda *a, **k: order.append("agent"),
    )
    monkeypatch.setattr(
        workspaces_manager,
        "delete_workspace",
        lambda *a, **k: order.append("workspace"),
    )
    vm = _vm_node(db)
    workspace = pending_workspace_node(
        db, cast("Config", object()), "ws1", vm, None
    )
    agent = _pending_agent(db, vm)
    log = RealizationLog()
    log.mark_realized(workspace)  # creation order: workspace, then agent
    log.mark_realized(agent)
    log.unwind()
    assert order == ["agent", "workspace"]
