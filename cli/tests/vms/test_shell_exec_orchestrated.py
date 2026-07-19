"""``vm shell`` / ``vm exec`` through the orchestrated model: the
derived graph with the env-target seam (env secrets join the boundary
via target registration, never the walk union), the gate-prompt parity
carries (these commands DO open the activation gate), the pre-gate
validation pins, and the VM scope reaching node readiness.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops, the reachability probe, and the SSH
transport are the fakes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.db import VMStatus
from agentworks.errors import ValidationError
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database

# A vm template with an env-block secret reference: a RUNTIME input
# for these commands, so it joins the boundary through the env-target
# registration (one prompt session with the site secret), never
# through the walk union.
VM_ENV_SECTION = """
[vm_templates.default.env]
API_KEY = { secret = "vm-env-secret" }
"""


@pytest.fixture(autouse=True)
def _env_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AW_SECRET_VM_ENV_SECRET", "env-val")


def _seed_vm(db: Database) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")


def _seed_workspace(db: Database, *, vm_name: str = "box", name: str = "ws1") -> None:
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


class _FakeTarget:
    def __init__(self) -> None:
        self.interactive_calls: list[tuple[str, dict[str, str]]] = []
        self.streaming_calls: list[tuple[str, dict[str, str]]] = []

    def interactive(self, cmd: str, *, env: dict[str, str] | None = None) -> int:
        self.interactive_calls.append((cmd, dict(env or {})))
        return 0

    def call_streaming(self, cmd: str, *, env: dict[str, str] | None = None) -> int:
        self.streaming_calls.append((cmd, dict(env or {})))
        return 0


@pytest.fixture
def target(monkeypatch: pytest.MonkeyPatch) -> _FakeTarget:
    fake = _FakeTarget()
    monkeypatch.setattr(
        "agentworks.transports.transport", lambda vm, config, **kwargs: fake
    )
    return fake


# -- the derived graph and the env-target seam --------------------------------


def test_graph_derives_from_row_and_env_joins_via_targets(
    db: Database, make_config  # noqa: ANN001
) -> None:
    """The shell / exec graph is the live VM alone (vm-site + vm), so
    the walk union is the site's config secret ONLY. The env-chain
    secret enters the boundary through the target registration seam,
    NOT the union: the distinction this pin exists to hold."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    config = make_config(VM_ENV_SECTION)
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    resolver = Resolver(config, registry)

    nodes = walk(live_vm_node(db, config, registry, vm))

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box"]
    assert secret_union(nodes) == ("proxmox-token",)

    for name in secret_union(nodes):
        resolver.register_name(name)
    scopes = vm_manager._resolve_vm_admin_env_scopes(registry, vm)
    resolver.register_targets(
        [vm_manager._vm_secret_target(scopes, label="vm-shell=box")]
    )
    resolver.resolve()
    assert set(resolver.values) == {"proxmox-token", "vm-env-secret"}


# -- gate-prompt parity (the per-command carries) -----------------------------


def test_shell_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """vm shell on a reachable VM: the gate's fast path costs nothing,
    so site + env secrets ride ONE boundary burst, and the composed
    env (with the resolved secret) reaches the interactive shell."""
    config = make_config(VM_ENV_SECTION)
    _seed_vm(db)
    _reachable(monkeypatch, True)

    # shell_vm returns the interactive exit code; the CLI owns process exit.
    assert vm_manager.shell_vm(db, config, "box") == 0

    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == ["proxmox-token", "vm-env-secret"]
    ((cmd, env),) = target.interactive_calls
    assert cmd == ""
    assert env.get("API_KEY") == "env-val"


def test_shell_stopped_vm_gate_burst_then_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """vm shell on a stopped VM: the gate's just-in-time token resolve
    (seeding the boundary), then ONE boundary burst for the env
    secrets; nothing resolves twice, nothing after."""
    config = make_config(VM_ENV_SECTION)
    _seed_vm(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    assert vm_manager.shell_vm(db, config, "box") == 0

    assert events == ["status", "start", "tailscale"]  # the gate ran
    assert resolve_counter == [["proxmox-token"], ["vm-env-secret"]]
    assert len(target.interactive_calls) == 1


def test_exec_reachable_vm_is_one_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config(VM_ENV_SECTION)
    _seed_vm(db)
    _reachable(monkeypatch, True)

    rc = vm_manager.exec_vm(db, config, "box", ["echo", "hi"])

    assert rc == 0
    assert len(resolve_counter) == 1
    assert sorted(resolve_counter[0]) == ["proxmox-token", "vm-env-secret"]
    ((cmd, env),) = target.streaming_calls
    assert cmd == "echo hi"
    assert env.get("API_KEY") == "env-val"


def test_exec_stopped_vm_gate_burst_then_boundary_burst(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config(VM_ENV_SECTION)
    _seed_vm(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    rc = vm_manager.exec_vm(db, config, "box", ["echo", "hi"])

    assert rc == 0
    assert events == ["status", "start", "tailscale"]
    assert resolve_counter == [["proxmox-token"], ["vm-env-secret"]]


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
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_vm(db)
    _no_gate(monkeypatch)

    with pytest.raises(ValidationError, match="cannot start with '-'"):
        vm_manager.exec_vm(db, config, "box", ["--workspace", "ws1", "pwd"])

    assert resolve_counter == []
    assert target.streaming_calls == []


def test_cross_vm_workspace_mismatch_fails_with_zero_resolves_and_zero_gate(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    _seed_vm(db)
    db.insert_vm("other", site="proxmox", hostname="other")
    _seed_workspace(db, vm_name="other", name="ws-other")
    _no_gate(monkeypatch)

    with pytest.raises(ValidationError, match="belongs to VM 'other', not 'box'"):
        vm_manager.exec_vm(
            db, config, "box", ["echo", "hi"], workspace_name="ws-other"
        )

    assert resolve_counter == []
    assert target.streaming_calls == []


# -- the operation scope reaches readiness ------------------------------------


def test_vm_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    vm_manager.exec_vm(db, config, "box", ["echo", "hi"])

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"
    assert scope.workspace is None and scope.agent is None and scope.session is None


# -- the held-active span -----------------------------------------------------


def test_shell_interactive_runs_inside_the_held_active_span(
    db: Database,
    make_config,  # noqa: ANN001
    target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """keep_active parity: the interactive session runs INSIDE the
    gate's held-active span (the WSL2 keepalive anchor), which closes
    after it."""
    import contextlib as _contextlib

    config = make_config()
    _seed_vm(db)
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

    assert vm_manager.shell_vm(db, config, "box") == 0

    assert events == ["hold-open", "interactive", "hold-close"]


# -- transport routing guards stay pre-composition ----------------------------


def test_shell_no_tailscale_fails_before_any_resolve(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.errors import StateError

    config = make_config()
    db.insert_vm("box", site="proxmox", hostname="box")  # no tailscale host
    _no_gate(monkeypatch)

    with pytest.raises(StateError, match="no Tailscale IP"):
        vm_manager.shell_vm(db, config, "box")
    assert resolve_counter == []


def test_shell_platform_transport_routes_through_the_node_platform(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """--platform: the native transport receives the SAME platform
    instance the node's site edge holds (one object per node)."""
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    seen: list[object] = []

    def _fake_native(vm: object, platform: object, cfg: object, *, stack: object) -> object:
        seen.append(platform)
        return SimpleNamespace(interactive=lambda cmd, **k: 0)

    monkeypatch.setattr("agentworks.transports.native_transport", _fake_native)

    def _no_transport(*a: object, **k: object) -> object:
        raise AssertionError("--platform must not route through Tailscale SSH")

    monkeypatch.setattr("agentworks.transports.transport", _no_transport)

    assert vm_manager.shell_vm(db, config, "box", platform_transport=True) == 0

    (platform,) = seen
    assert isinstance(platform, ProxmoxPlatform)
