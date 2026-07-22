"""``agent delete`` / ``agent grant-workspaces`` /
``agent revoke-workspaces`` through the orchestrated model: the shared
derived graph (the live VM alone, no env-chain targets), the
gate-prompt parity carries (all three DO open the activation gate),
the pre-gate validation pins (refusals cost zero prompts, zero
resolves, zero gate events), delete's two paths (its own
``gated_vm_boundary`` composition when standalone; the caller's bound
platform held verbatim on the nested-teardown path), and the DB /
on-VM choreography preserved from the imperative bodies.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops, the reachability probe, and the
admin SSH transport are the fakes.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.agents import grants as agent_grants
from agentworks.agents import manager as agent_manager
from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.db import VMStatus
from agentworks.errors import (
    NotFoundError,
    StateError,
    UserAbort,
    ValidationError,
)
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database
    from tests.conftest import CapturedOutput


def _seed(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.insert_agent("a1", "box", "agt-a1", template="default")


def _seed_workspace(db: Database, *, vm_name: str, name: str) -> None:
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) VALUES (?, ?, ?, ?)",
        (name, vm_name, f"/srv/{name}", f"ws-{name}"),
    )
    db._conn.commit()


def _seed_live_session(db: Database, *, name: str, ws: str, agent: str) -> None:
    """A session row that reads as alive (pid + boot_id + socket), so
    delete's status-aware kill loop probes and kills it."""
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, "
        "agent_name, socket_path, pid, boot_id) VALUES (?, ?, 'default', "
        "'agent', ?, ?, 4242, 'boot-1')",
        (name, ws, agent, f"/tmp/{name}.sock"),
    )
    db._conn.commit()


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _stop_the_vm(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    _reachable(monkeypatch, False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row, ctx: events.append("status") or VMStatus.STOPPED,
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start"))
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale"))


def _no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_status(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for a command that must fail pre-gate")

    monkeypatch.setattr(ProxmoxPlatform, "status", _no_status)
    _reachable(monkeypatch, False)


def _node_holding(db: Database, config: object, platform: object, *, vm_name: str = "box"):  # noqa: ANN202
    """A live VM node for ``vm_name`` (default 'box') whose site holds
    the given platform: the shape a nested teardown hands
    ``delete_agent`` (it re-enters the hold through
    ``vm_node.site.platform``)."""
    from agentworks.vms.nodes import LiveVMNode, VMSiteNode

    row = db.get_vm(vm_name)
    assert row is not None
    site = VMSiteNode("proxmox", platform, (), object())  # type: ignore[arg-type]
    return LiveVMNode(db, config, object(), row, site)  # type: ignore[arg-type]


class _FakeAdminTarget:
    """Admin transport double: every command is recorded and answers
    ok, so tmux probes / kills, group ops, and the user delete all
    proceed."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        self.commands.append(cmd)
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> _FakeAdminTarget:
    """One recording admin target behind every module that captured the
    eager ``transport`` import (manager's session kill, grants' group
    ops, initializer's user delete)."""
    fake = _FakeAdminTarget()
    factory = lambda vm, config, **kwargs: fake  # noqa: E731
    for mod in (
        "agentworks.agents.manager.transport",
        "agentworks.agents.grants.transport",
        "agentworks.agents.initializer.transport",
    ):
        monkeypatch.setattr(mod, factory)
    return fake


@pytest.fixture
def synced(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record (and neutralize) the operator SSH-config refresh."""
    calls: list[object] = []
    monkeypatch.setattr(
        "agentworks.ssh_config.sync_ssh_config",
        lambda *a, **k: calls.append(a),
    )
    return calls


# -- the derived graph (stated once for the trio) -----------------------------


def test_graph_is_the_live_vm_alone_for_the_trio(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    """delete / grant / revoke share one graph: the live VM from its
    row (vm-site + vm), union = the site's config secret only. No
    agent node exists (nothing agent-shaped is provisioned) and no
    env-chain target registers (none of the three composes runtime
    env), so the boundary is exactly the walk union."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    _seed(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    resolver = Resolver(config, registry)

    nodes = walk(live_vm_node(db, config, registry, vm))

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box"]
    assert secret_union(nodes) == ("proxmox-token",)

    for name in secret_union(nodes):
        resolver.register_name(name)
    resolver.resolve()
    assert set(resolver.values) == {"proxmox-token"}


# -- gate-prompt parity (the per-command carries) -----------------------------


def test_grant_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    _reachable(monkeypatch, True)

    agent_grants.grant_workspaces(db, config, agent_name="a1", workspace_names=["ws1"])

    assert resolve_counter == [["proxmox-token"]]
    assert db.has_any_grant("a1", "ws1")
    assert "Granted: ws1" in captured_output.info
    # The per-workspace lines stay info; the command's summary is a result().
    from agentworks.output import Role

    assert (Role.RESULT, 0, "Agent 'a1' granted access to 1 workspace") in (captured_output.lines)


def test_grant_stopped_vm_gate_burst_seeds_the_whole_union(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """No env targets, so the gate's just-in-time resolve covers the
    entire union: one burst, nothing twice, nothing after."""
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    agent_grants.grant_workspaces(db, config, agent_name="a1", workspace_names=["ws1"])

    assert events == ["status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"]]
    assert db.has_any_grant("a1", "ws1")


def test_revoke_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    db.insert_agent_grant("a1", "ws1", "explicit")
    _reachable(monkeypatch, True)

    agent_grants.revoke_workspaces(db, config, agent_name="a1", workspace_names=["ws1"])

    assert resolve_counter == [["proxmox-token"]]
    assert not db.has_any_grant("a1", "ws1")
    assert "Revoked: ws1" in captured_output.info
    from agentworks.output import Role

    assert (Role.RESULT, 0, "Revoked 1 workspace grant from agent 'a1'") in (captured_output.lines)


def test_revoke_stopped_vm_gate_burst_seeds_the_whole_union(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    db.insert_agent_grant("a1", "ws1", "explicit")
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    agent_grants.revoke_workspaces(db, config, agent_name="a1", workspace_names=["ws1"])

    assert events == ["status", "start", "tailscale"]
    assert resolve_counter == [["proxmox-token"]]
    assert not db.has_any_grant("a1", "ws1")


def test_delete_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    synced: list[object],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)

    agent_manager.delete_agent(db, config, name="a1", yes=True)

    assert resolve_counter == [["proxmox-token"]]
    assert db.get_agent("a1") is None


def test_delete_stopped_vm_gate_burst_seeds_the_whole_union(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    synced: list[object],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    agent_manager.delete_agent(db, config, name="a1", yes=True)

    assert events == ["status", "start", "tailscale"]
    assert resolve_counter == [["proxmox-token"]]
    assert db.get_agent("a1") is None


# -- validation stays pre-gate ------------------------------------------------


def test_delete_sessions_guard_refuses_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    _seed_live_session(db, name="s1", ws="ws1", agent="a1")
    _no_gate(monkeypatch)

    with pytest.raises(StateError, match="has 1 session"):
        agent_manager.delete_agent(db, config, name="a1")

    assert resolve_counter == []
    assert target.commands == []
    assert db.get_agent("a1") is not None


def test_delete_declined_confirm_aborts_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)
    captured_output.confirm_response = False

    with pytest.raises(UserAbort, match="delete cancelled"):
        agent_manager.delete_agent(db, config, name="a1")

    assert resolve_counter == []
    assert target.commands == []
    assert db.get_agent("a1") is not None


def test_grant_empty_request_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)

    with pytest.raises(ValidationError, match="needs at least one workspace name"):
        agent_grants.grant_workspaces(db, config, agent_name="a1", workspace_names=[])

    assert resolve_counter == []
    assert target.commands == []


def test_revoke_unknown_agent_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)

    with pytest.raises(NotFoundError, match="agent 'ghost' not found"):
        agent_grants.revoke_workspaces(db, config, agent_name="ghost", workspace_names=["ws1"])

    assert resolve_counter == []
    assert target.commands == []


# -- the operation scope reaches readiness ------------------------------------


def test_agent_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    agent_grants.grant_workspaces(db, config, agent_name="a1", workspace_names=["ws1"])

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.AGENT
    assert scope.vm == "box"
    assert scope.agent == "a1"
    assert scope.workspace is None and scope.session is None


# -- delete choreography (the standalone path) --------------------------------


def test_delete_choreography_end_to_end_standalone(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAdminTarget,
    synced: list[object],
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """The imperative delete shape, end to end: the live session is
    status-probed and killed, its row cascades, the group membership
    goes, the on-VM user delete runs, the agent row goes, and the
    operator SSH config refreshes; messages as at HEAD."""
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    db.insert_agent_grant("a1", "ws1", "explicit")
    _seed_live_session(db, name="s1", ws="ws1", agent="a1")
    _reachable(monkeypatch, True)

    agent_manager.delete_agent(db, config, name="a1", force=True, yes=True)

    assert any("has-session -t s1" in c for c in target.commands)
    assert any("kill-session -t s1" in c for c in target.commands)
    assert any("gpasswd -d agt-a1 ws-ws1" in c for c in target.commands)
    assert any("pkill -u agt-a1" in c for c in target.commands)
    assert any("userdel -r agt-a1" in c for c in target.commands)
    assert db.get_session("s1") is None
    assert db.get_agent("a1") is None
    assert len(synced) == 1
    assert "Deleting agent 'a1' on VM 'box'..." in captured_output.info
    assert "Agent 'a1' deleted" in captured_output.info
    assert "Deleted 1 session(s)" in captured_output.detail


def test_delete_nested_platform_path_reuses_the_callers_composition(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAdminTarget,
    synced: list[object],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The nested-teardown seam: the caller hands its already-held VM
    NODE, whose gate has converged and holds the VM, so delete performs
    ZERO additional resolves and composes no second boundary (a status
    probe would be one); it trusts that gate and re-enters only the
    keepalive hold, reaching the platform through the node's site edge."""

    class _BoundPlatformStub:
        name = "proxmox"

        def __init__(self) -> None:
            self.holds = 0

        def vm_active(self, row: object, *, config: object | None = None) -> contextlib.AbstractContextManager[None]:
            self.holds += 1
            return contextlib.nullcontext()

        def status(self, row: object, ctx: object) -> VMStatus:
            raise AssertionError("nested delete must not probe status")

    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)  # any boundary composition would probe status and fail
    _reachable(monkeypatch, True)
    bound = _BoundPlatformStub()
    vm_node = _node_holding(db, config, bound)

    agent_manager.delete_agent(
        db,
        config,
        name="a1",
        force=True,
        yes=True,
        vm_node=vm_node,
    )

    assert resolve_counter == []  # nothing resolved beyond the caller's pass
    assert bound.holds == 1  # the hold was re-entered, nothing else
    assert db.get_agent("a1") is None
    assert any("userdel -r agt-a1" in c for c in target.commands)


def test_delete_nested_rejects_a_mismatched_vm_node(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    synced: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforce-invariants pin: a teardown must hand ``delete_agent`` the
    agent's OWN vm node. A node for a different VM would hold that VM
    active while the delete body issues SSH + DB work against the agent's
    real VM, a silent footgun; the guard raises a typed ``StateError``
    before the hold is ever entered."""
    config = make_config()
    _seed(db)  # a1 on 'box'
    # A live node for a DIFFERENT VM than a1's ('box').
    db.insert_vm("other", site="proxmox", hostname="other")
    db.update_vm_tailscale("other", "100.64.0.10")
    _no_gate(monkeypatch)  # nothing may probe status or hold the VM
    vm_node = _node_holding(db, config, object(), vm_name="other")

    with pytest.raises(StateError, match="teardown-wiring bug"):
        agent_manager.delete_agent(
            db,
            config,
            name="a1",
            force=True,
            yes=True,
            vm_node=vm_node,
        )

    assert resolve_counter == []  # refused before any resolve
    assert db.get_agent("a1") is not None  # nothing was deleted
    assert synced == []  # the SSH-config refresh never ran


# -- grant / revoke choreography ----------------------------------------------


def test_grant_all_sets_flag_and_adds_every_vm_workspace(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    _seed_workspace(db, vm_name="box", name="ws2")
    db.insert_vm("other", site="proxmox", hostname="other")
    _seed_workspace(db, vm_name="other", name="elsewhere")
    _reachable(monkeypatch, True)

    agent_grants.grant_workspaces(db, config, agent_name="a1", workspace_names=[], grant_all=True)

    row = db.get_agent("a1")
    assert row is not None and row.grant_all
    assert db.has_any_grant("a1", "ws1") and db.has_any_grant("a1", "ws2")
    assert not db.has_any_grant("a1", "elsewhere")  # other VM untouched
    assert any("usermod -aG ws-ws1 agt-a1" in c for c in target.commands)
    assert any("usermod -aG ws-ws2 agt-a1" in c for c in target.commands)
    assert not any("elsewhere" in c for c in target.commands)
    assert "Agent 'a1' granted access to all workspaces" in captured_output.info


def test_grant_missing_workspace_warns_and_skips(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    _reachable(monkeypatch, True)

    agent_grants.grant_workspaces(db, config, agent_name="a1", workspace_names=["ws1", "nope"])

    assert db.has_any_grant("a1", "ws1")
    assert not db.has_any_grant("a1", "nope")
    assert "workspace 'nope' not found, skipping" in captured_output.warnings
    assert "Granted: ws1" in captured_output.info


def test_revoke_named_workspace_with_implicit_access_keeps_membership(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Revoking the explicit grant while a session's implicit grant
    remains keeps the group membership (it backs the remaining grant)
    and says so."""
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    db.insert_agent_grant("a1", "ws1", "explicit")
    db.insert_agent_grant("a1", "ws1", "implicit", session_name="s1")
    _reachable(monkeypatch, True)

    agent_grants.revoke_workspaces(db, config, agent_name="a1", workspace_names=["ws1"])

    assert db.has_any_grant("a1", "ws1")  # the implicit grant survives
    assert not any("gpasswd" in c for c in target.commands)
    assert "Revoked: ws1 (still has implicit access via sessions)" in captured_output.info


def test_revoke_all_warns_about_remaining_implicit_access(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAdminTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """revoke --all clears the flag and every explicit grant, removes the
    agent from the group of any workspace with no grant left, and warns
    about the workspaces still implicitly reachable via sessions.

    Fixed behavior (issue #189): the granted-workspaces snapshot is now
    taken BEFORE the explicit rows are deleted, so a workspace whose only
    grant was explicit stays in the snapshot and its on-VM group
    membership is removed (what the help text implies). Previously the
    snapshot ran after the delete, so such a workspace was absent from it
    and its membership survived the revoke."""
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")
    _seed_workspace(db, vm_name="box", name="ws2")
    db.update_agent_grant_all("a1", True)
    db.insert_agent_grant("a1", "ws1", "explicit")
    db.insert_agent_grant("a1", "ws1", "implicit", session_name="s1")
    db.insert_agent_grant("a1", "ws2", "explicit")
    _reachable(monkeypatch, True)

    agent_grants.revoke_workspaces(db, config, agent_name="a1", workspace_names=[], revoke_all=True)

    row = db.get_agent("a1")
    assert row is not None and not row.grant_all
    assert db.has_any_grant("a1", "ws1")  # implicit survives
    assert not db.has_any_grant("a1", "ws2")
    # ws2's only grant was explicit, so with it gone the agent is removed
    # from ws2's group; ws1 still has its implicit grant, so it is not.
    assert any("gpasswd -d agt-a1 ws-ws2" in c for c in target.commands)
    assert not any("ws-ws1" in c for c in target.commands)
    assert "All explicit grants revoked for agent 'a1'" in captured_output.info
    assert "agent still has implicit access via sessions to: ws1" in captured_output.warnings
