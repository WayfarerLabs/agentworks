"""``agent shell`` / ``agent exec`` through the orchestrated model:
the derived graph with the env-target seam (agent env secrets join the
boundary via target registration, never the walk union), the
gate-prompt parity carries (these commands DO open the activation
gate), the pre-gate authz validation pins, and the VM scope reaching
node readiness.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops, the reachability probe, and the
agent SSH transport are the fakes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.agents import manager as agent_manager
from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.db import VMStatus
from agentworks.errors import AuthorizationError, ValidationError
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database

# An agent template with an env-block secret reference: a RUNTIME
# input for these commands, joining the boundary through the
# env-target registration (one prompt session with the site secret),
# never through the walk union.
AGENT_ENV_SECTION = """
[agent_templates.default.env]
AGENT_TOKEN = { secret = "agent-env-secret" }
"""


@pytest.fixture(autouse=True)
def _env_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_AGENT_ENV_SECRET", "agent-env-val")


def _seed(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.insert_agent("a1", "box", "agt-a1", template="default")


def _seed_workspace(db: Database, *, vm_name: str, name: str) -> None:
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES (?, ?, ?, ?)",
        (name, vm_name, f"/srv/{name}", f"ws-{name}"),
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
    monkeypatch.setattr(
        ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start")
    )
    monkeypatch.setattr(
        vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale")
    )


class _FakeAgentTarget:
    """Agent transport double: the SSH probe answers ok, interactive
    and streaming calls are recorded with their env."""

    def __init__(self) -> None:
        self.interactive_calls: list[tuple[str, dict[str, str]]] = []
        self.streaming_calls: list[tuple[str, dict[str, str]]] = []

    def run(self, cmd: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="")

    def interactive(self, cmd: str, *, env: dict[str, str] | None = None) -> int:
        self.interactive_calls.append((cmd, dict(env or {})))
        return 0

    def call_streaming(self, cmd: str, *, env: dict[str, str] | None = None) -> int:
        self.streaming_calls.append((cmd, dict(env or {})))
        return 0


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> _FakeAgentTarget:
    fake = _FakeAgentTarget()
    monkeypatch.setattr(
        "agentworks.transports.agent_transport",
        lambda vm, config, agent, **kwargs: fake,
    )
    return fake


# -- the derived graph and the env-target seam --------------------------------


def test_graph_derives_from_row_and_env_joins_via_targets(
    db: Database, make_config  # noqa: ANN001
) -> None:
    """The agent shell / exec graph is the live VM alone (vm-site +
    vm): no agent node exists (nothing about the agent is provisioned
    here), so the walk union is the site's config secret ONLY. The
    agent template's env secret enters the boundary through the target
    registration seam, NOT the union."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    config = make_config(AGENT_ENV_SECTION)
    _seed(db)
    vm = db.get_vm("box")
    agent = db.get_agent("a1")
    assert vm is not None and agent is not None
    registry = build_registry(config)
    resolver = Resolver(config, registry)

    nodes = walk(live_vm_node(db, config, registry, vm))

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box"]
    assert secret_union(nodes) == ("proxmox-token",)

    for name in secret_union(nodes):
        resolver.register_name(name)
    scopes = agent_manager._resolve_agent_direct_env_scopes(registry, vm, agent)
    resolver.register_targets(
        [agent_manager._agent_direct_secret_target(scopes, label="agent-shell=a1")]
    )
    resolver.resolve()
    assert set(resolver.values) == {"proxmox-token", "agent-env-secret"}


# -- gate-prompt parity (the per-command carries) -----------------------------


def test_shell_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAgentTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config(AGENT_ENV_SECTION)
    _seed(db)
    _reachable(monkeypatch, True)

    # shell_agent returns the interactive exit code; the CLI owns process exit.
    assert agent_manager.shell_agent(db, config, name="a1") == 0

    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == ["agent-env-secret", "proxmox-token"]
    ((cmd, env),) = target.interactive_calls
    assert cmd == ""
    assert env.get("AGENT_TOKEN") == "agent-env-val"


def test_shell_stopped_vm_gate_burst_then_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAgentTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config(AGENT_ENV_SECTION)
    _seed(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    agent_manager.shell_agent(db, config, name="a1")

    assert events == ["status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"], ["agent-env-secret"]]
    assert len(target.interactive_calls) == 1


def test_exec_stopped_vm_gate_burst_then_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAgentTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config(AGENT_ENV_SECTION)
    _seed(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    rc = agent_manager.exec_agent(db, config, name="a1", command=["echo", "hi"])

    assert rc == 0
    assert events == ["status", "start", "tailscale"]
    assert resolve_counter == [["proxmox-token"], ["agent-env-secret"]]
    ((cmd, env),) = target.streaming_calls
    assert cmd.startswith("$SHELL -lc ")
    assert env.get("AGENT_TOKEN") == "agent-env-val"


# -- validation stays pre-gate ------------------------------------------------


def _no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_status(self: ProxmoxPlatform, row: object) -> VMStatus:
        raise AssertionError("the gate ran for a command that must fail pre-gate")

    monkeypatch.setattr(ProxmoxPlatform, "status", _no_status)
    _reachable(monkeypatch, False)


def test_exec_dash_prefixed_command_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAgentTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed(db)
    _no_gate(monkeypatch)

    with pytest.raises(ValidationError, match="cannot start with '-'"):
        agent_manager.exec_agent(
            db, config, name="a1", command=["--workspace", "ws1", "pwd"]
        )

    assert resolve_counter == []
    assert target.streaming_calls == []


def test_missing_grant_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeAgentTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The authz-bearing workspace resolution stays pre-gate: an
    ungranted workspace refuses before any prompt or VM start."""
    config = make_config()
    _seed(db)
    _seed_workspace(db, vm_name="box", name="ws1")  # no grant for a1
    _no_gate(monkeypatch)

    with pytest.raises(AuthorizationError, match="does not have access"):
        agent_manager.exec_agent(
            db, config, name="a1", command=["echo", "hi"], workspace_name="ws1"
        )

    assert resolve_counter == []
    assert target.streaming_calls == []


# -- the held-active span -----------------------------------------------------


def test_shell_interactive_runs_inside_the_held_active_span(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAgentTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """keep_active parity: the interactive session runs INSIDE the
    gate's held-active span (the WSL2 keepalive anchor), which closes
    after it. The mirror of the vm-domain pin in
    tests/vms/test_shell_exec_orchestrated.py."""
    import contextlib as _contextlib

    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)
    events: list[str] = []

    @_contextlib.contextmanager
    def _recording_hold(self: ProxmoxPlatform, row: object, *, config: object = None):  # noqa: ANN202
        events.append("hold-open")
        try:
            yield
        finally:
            events.append("hold-close")

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _recording_hold)
    interactive = target.interactive

    def _tracking(cmd: str, *, env: dict[str, str] | None = None) -> int:
        events.append("interactive")
        return interactive(cmd, env=env)

    target.interactive = _tracking  # type: ignore[method-assign]

    agent_manager.shell_agent(db, config, name="a1")

    assert events == ["hold-open", "interactive", "hold-close"]


# -- the operation scope reaches readiness ------------------------------------


def test_agent_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeAgentTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed(db)
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    agent_manager.exec_agent(db, config, name="a1", command=["echo", "hi"])

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.AGENT
    assert scope.vm == "box"
    assert scope.agent == "a1"
    assert scope.workspace is None and scope.session is None
