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


def _stub_platform_ctx():  # noqa: ANN202 - test helper
    """The teardown ctx source the orchestrator would hand in: these
    nodes' teardown paths patch delete_agent / delete_workspace or run
    against stub platforms, so an empty op-start context suffices."""
    from agentworks.capabilities.base import RunContext

    return RunContext()


def _vm_node(db: Database, name: str = "box") -> LiveVMNode:
    db.insert_vm(name, site="stub", hostname=name)
    row = db.get_vm(name)
    assert row is not None
    site = VMSiteNode("stub", cast("VMPlatform", _Platform()), (), cast("Registry", object()))
    return LiveVMNode(
        db, cast("Config", object()), cast("Registry", object()), row, site
    )


def _pending_agent(db: Database, vm: LiveVMNode, name: str = "dev"):
    from agentworks.agents.nodes import AgentTemplateNode, pending_agent_node
    from agentworks.agents.templates import ResolvedAgentTemplate

    template = AgentTemplateNode(ResolvedAgentTemplate(name="default"), ())
    return pending_agent_node(
        db, cast("Config", object()), name, template, vm, _stub_platform_ctx
    )


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
        db, cast("Config", object()), "ws1", vm_node, None, _stub_platform_ctx
    )
    template = ResolvedSessionTemplate(
        name="claude", required_commands=list(required)
    )
    return pending_session_node(
        db,
        cast("Config", object()),
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


def test_scope_less_context_is_a_loud_error(db: Database) -> None:
    """A context with NO scope is an orchestrator bug, not an
    out-of-scope level: skipping would silently disable the check
    forever (the imperative call always ran)."""
    session = _session(db, agent=None, admin=True)
    with pytest.raises(StateError, match="no operation scope"):
        session.preflight(RunContext())


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
        vm_name="box",
    )
    with pytest.raises(StateError, match="refusing to skip"):
        check.preflight(_ctx())


def test_admin_mode_probes_the_admin_target(db: Database) -> None:
    session = _session(db, agent=None, admin=True)
    probe = _Probe()
    session.preflight(_ctx(agent=None, admin=True, admin_target=probe))
    assert len(probe.commands) == 1


def test_admin_mode_error_names_the_vm(db: Database) -> None:
    """Label parity with the imperative call sites: admin sessions
    name the VM, not the agent."""
    session = _session(db, agent=None, admin=True)
    probe = _Probe(missing={"claude"})
    with pytest.raises(StateError, match="requires 'claude'") as exc:
        session.preflight(_ctx(agent=None, admin=True, admin_target=probe))
    assert "for VM 'box'." in str(exc.value)


@pytest.mark.parametrize("admin_mode", [False, True])
def test_missing_transport_defers_at_preflight_and_is_loud_at_runup(
    db: Database, admin_mode: bool
) -> None:
    """A probe-ready target with no transport on the command-start
    context defers (the stage's timing did not carry it); the op-start
    context must carry it, so runup without one is a loud error. Same
    shape in agent and admin mode."""
    vm = _vm_node(db)
    if admin_mode:
        session = _session(db, agent=None, admin=True, vm=vm)
        ctx = _ctx(agent=None, admin=True)
    else:
        agent = _pending_agent(db, vm)
        agent.mark_realized()
        session = _session(db, agent=agent, vm=vm)
        ctx = _ctx()

    session.preflight(ctx)  # no transport: defer, no raise
    with pytest.raises(StateError, match="op-start context"):
        session.runup(ctx)


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
    from agentworks.git_credentials.nodes import GitCredentialNode
    from agentworks.orchestration.walk import walk
    from agentworks.workspaces.nodes import pending_workspace_node

    vm = _vm_node(db)
    provider = SimpleNamespace(
        owner_name="gh", secret_name="git-token-gh",
        preflight=lambda ctx: None, runup=lambda ctx: None,
    )
    cred = GitCredentialNode(
        "gh",
        provider,  # type: ignore[arg-type]
        (SimpleNamespace(name="git-token-gh", usage="the auth token"),),  # type: ignore[arg-type]
        cast("Registry", object()),
    )
    template = AgentTemplateNode(
        ResolvedAgentTemplate(name="default", git_credentials=["gh"]), (cred,)
    )
    agent = pending_agent_node(
        db, cast("Config", object()), "dev", template, vm, _stub_platform_ctx
    )
    workspace = pending_workspace_node(
        db, cast("Config", object()), "ws1", vm, None, _stub_platform_ctx
    )
    session = pending_session_node(
        db,
        cast("Config", object()),
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
        db, cast("Config", object()), "ws1", vm, None, _stub_platform_ctx
    )
    workspace.mark_realized()
    workspace.teardown()
    (call,) = calls
    assert call["name"] == "ws1"
    assert call["force"] is True and call["yes"] is True
    assert call["platform"] is vm.site.platform


def _seed_session_partial_state(
    db: Database, *, with_grant: bool = True
) -> None:
    """Seed the artifacts a mid-slice failure can leave behind: the
    workspace row (pre-existing), the agent row, the implicit grant,
    and the session row."""
    from agentworks.db import SessionMode

    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'box', '/srv/ws1', 'ws-ws1')"
    )
    db._conn.commit()
    db.insert_agent("dev", "box", "agt-dev")
    if with_grant:
        db.insert_agent_grant("dev", "ws1", "implicit", session_name="s1")
    db.insert_session(
        "s1", "ws1", "claude", SessionMode.AGENT,
        agent_name="dev", socket_path="/tmp/s1.sock",
    )


def test_pending_session_teardown_is_todays_rollback_body(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session's partial-state cleaner: delete the row, revoke the
    implicit grant, and (no other grant remaining) remove the agent
    from the workspace group, exactly the imperative session-internal
    rollback."""
    from agentworks.agents import grants as agents_grants

    removed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agents_grants,
        "remove_from_workspace_group",
        lambda vm, config, db_, linux_user, ws, **k: removed.append(
            (linux_user, ws)
        ),
    )
    vm = _vm_node(db)
    agent = _pending_agent(db, vm)
    session = _session(db, agent=agent, vm=vm)
    _seed_session_partial_state(db)

    session.teardown()

    assert db.get_session("s1") is None
    assert not db.has_any_grant("dev", "ws1")
    assert removed == [("agt-dev", "ws1")]


def test_pending_session_teardown_keeps_group_with_other_grants(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit grant survives the implicit revoke, so the group
    membership stays (it backs the remaining grant)."""
    from agentworks.agents import grants as agents_grants

    removed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agents_grants,
        "remove_from_workspace_group",
        lambda *a, **k: removed.append(("called", "")),
    )
    vm = _vm_node(db)
    agent = _pending_agent(db, vm)
    session = _session(db, agent=agent, vm=vm)
    _seed_session_partial_state(db)
    db.insert_agent_grant("dev", "ws1", "explicit")

    session.teardown()

    assert db.get_session("s1") is None
    assert db.has_any_grant("dev", "ws1")  # the explicit grant remains
    assert removed == []


def test_pending_session_teardown_admin_mode_deletes_only_the_row(
    db: Database,
) -> None:
    from agentworks.db import SessionMode

    session = _session(db, agent=None, admin=True)
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws1', 'box', '/srv/ws1', 'ws-ws1')"
    )
    db._conn.commit()
    db.insert_session("s1", "ws1", "claude", SessionMode.ADMIN)

    session.teardown()

    assert db.get_session("s1") is None


def test_pending_session_teardown_warns_and_never_raises(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output
) -> None:
    """The cleaner runs inside an exception path on PARTIAL state, so a
    failing step warns and the teardown continues; raising would mask
    the original error."""
    from agentworks.db import Database as _Db

    monkeypatch.setattr(
        _Db,
        "delete_session",
        lambda self, name: (_ for _ in ()).throw(RuntimeError("db locked")),
    )
    session = _session(db, agent=None, admin=True)

    session.teardown()  # no raise

    (warning,) = [w for w in captured_output.warnings if "rollback" in w]
    assert "failed to delete session row 's1'" in warning
    assert "db locked" in warning


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
        db, cast("Config", object()), "ws1", vm, None, _stub_platform_ctx
    )
    agent = _pending_agent(db, vm)
    log = RealizationLog()
    log.mark_realized(workspace)  # creation order: workspace, then agent
    log.mark_realized(agent)
    log.unwind()
    assert order == ["agent", "workspace"]


# -- the live halves ---------------------------------------------------------


def _live_agent(db: Database, vm: LiveVMNode, name: str = "dev"):
    from agentworks.agents.nodes import live_agent_node
    from agentworks.db import AgentRow

    row = AgentRow(
        name=name, vm_name=vm.row.name, linux_user=f"agw-{name}",
        template=None, grant_all=False, created_at="",
    )
    return live_agent_node(row, vm)


def _session_row(*, agent_name: str | None) -> object:
    from agentworks.db import SessionRow

    return SessionRow(
        name="s1", workspace_name="ws1", template="claude",
        mode="agent" if agent_name else "admin",
        created_at="", updated_at="", agent_name=agent_name,
    )


def _live_workspace(db: Database, vm: LiveVMNode):
    from agentworks.db import WorkspaceRow
    from agentworks.workspaces.nodes import live_workspace_node

    row = WorkspaceRow(
        name="ws1", vm_name=vm.row.name, template=None,
        workspace_path="/srv/ws1", created_at="",
        linux_group="ws-ws1",
    )
    return live_workspace_node(row, vm)


def test_live_agent_and_workspace_nodes_shape(db: Database) -> None:
    vm = _vm_node(db)
    agent = _live_agent(db, vm)
    workspace = _live_workspace(db, vm)
    assert agent.key == "agent/dev"
    assert agent.realized is True  # a live node IS realized
    assert agent.deps() == (vm,)
    assert workspace.key == "workspace/ws1"
    assert workspace.deps() == (vm,)


def test_live_session_probes_its_realized_agent_at_preflight(
    db: Database,
) -> None:
    from agentworks.sessions.nodes import live_session_node

    vm = _vm_node(db)
    agent = _live_agent(db, vm)
    session = live_session_node(
        _session_row(agent_name="dev"),  # type: ignore[arg-type]
        ResolvedSessionTemplate(name="claude", required_commands=["claude"]),
        agent=agent,
        workspace=_live_workspace(db, vm),
        vm=vm,
    )
    probe = _Probe()
    session.preflight(_ctx(agent_target=probe))
    assert len(probe.commands) == 1  # realized target: the early probe


def test_live_session_admin_mode_comes_from_the_row(db: Database) -> None:
    from agentworks.sessions.nodes import live_session_node

    vm = _vm_node(db)
    session = live_session_node(
        _session_row(agent_name=None),  # type: ignore[arg-type]
        ResolvedSessionTemplate(name="claude", required_commands=["claude"]),
        agent=None,
        workspace=_live_workspace(db, vm),
        vm=vm,
    )
    probe = _Probe(missing={"claude"})
    with pytest.raises(StateError, match="for VM 'box'"):
        session.preflight(_ctx(agent=None, admin=True, admin_target=probe))


def test_live_session_agent_row_with_no_agent_node_is_loud(
    db: Database,
) -> None:
    """The fork's loud branch must survive the live factory: an
    agent-mode row handed no agent node is a lookup bug, never a
    silent fall-back to admin."""
    from agentworks.sessions.nodes import live_session_node

    vm = _vm_node(db)
    with pytest.raises(StateError, match="refusing to fall back"):
        live_session_node(
            _session_row(agent_name="dev"),  # type: ignore[arg-type]
            ResolvedSessionTemplate(name="claude"),
            agent=None,
            workspace=_live_workspace(db, vm),
            vm=vm,
        )


def test_live_session_admin_row_with_an_agent_node_is_loud(
    db: Database,
) -> None:
    from agentworks.sessions.nodes import live_session_node

    vm = _vm_node(db)
    with pytest.raises(StateError, match="admin session"):
        live_session_node(
            _session_row(agent_name=None),  # type: ignore[arg-type]
            ResolvedSessionTemplate(name="claude"),
            agent=_live_agent(db, vm),
            workspace=_live_workspace(db, vm),
            vm=vm,
        )


def test_live_session_agent_name_mismatch_is_loud(db: Database) -> None:
    from agentworks.sessions.nodes import live_session_node

    vm = _vm_node(db)
    with pytest.raises(StateError, match="must agree"):
        live_session_node(
            _session_row(agent_name="dev"),  # type: ignore[arg-type]
            ResolvedSessionTemplate(name="claude"),
            agent=_live_agent(db, vm, name="other"),
            workspace=_live_workspace(db, vm),
            vm=vm,
        )


# -- the agent-template factory (declared references become edges) -----------


def test_agent_template_node_derives_credential_edges(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The factory obligation, proven through real declared resources:
    one git-credential node per declared name, in declaration order,
    each carrying its token secret."""
    from agentworks.agents.nodes import agent_template_node
    from agentworks.agents.templates import resolve_template
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config

    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n'
        '[git_credentials.gh]\nprovider = "github"\n'
        '[git_credentials.gh2]\nprovider = "github"\n'
        "[agent_templates.default]\n"
        'git_credentials = ["gh", "gh2"]\n'
    )
    config = load_config(cfg, warn_issues=False, warn_deprecations=False)
    registry = build_registry(config)
    tmpl = resolve_template(registry, None)
    node = agent_template_node(registry, tmpl)
    assert node.key == "agent-template/default"
    assert [c.key for c in node.deps()] == [
        "git-credential/gh",
        "git-credential/gh2",
    ]
    assert node.credentials[0].secret_refs() == ("git-token-gh",)
    assert node.credentials[1].secret_refs() == ("git-token-gh2",)
    assert node.secret_refs() == ()  # tokens ride the credential nodes
