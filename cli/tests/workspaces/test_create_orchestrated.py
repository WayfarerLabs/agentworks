"""``workspace create`` through the orchestrated model: the derived
graph, the gate-prompt parity carry (the tracer's mirror shape), the
failure parity with the imperative command, and the WORKSPACE scope
reaching node readiness.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend ops, the reachability probe, and the on-VM
mutation (the workspace VM backend) are the fakes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.errors import ExternalError
from agentworks.vms import manager as vm_manager
from agentworks.workspaces import manager as workspace_manager

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database

# A workspace template with an env-block secret reference: a RUNTIME
# input (delivered where sessions run), which must never join the
# provisioning union (the hermeticity pin).
WORKSPACE_ENV_SECTION = """
[workspace_templates.default.env]
WS_TOKEN = { secret = "ws-env-secret" }
"""


@pytest.fixture
def mutation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the on-VM mutation (the workspace VM backend); capture what
    the realize body hands it."""
    captured: dict[str, Any] = {}

    def _fake_create(
        vm: Any, config: Any, ws_name: str, template: Any, *, logger: Any = None
    ) -> str:
        captured["ws_name"] = ws_name
        captured["template"] = template.name
        return f"/srv/{ws_name}"

    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.create_vm_workspace", _fake_create
    )
    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.generate_vscode_workspace",
        lambda vm, config, ws_name, path: f"/tmp/{ws_name}.code-workspace",
    )
    return captured


def _seed_vm(db: Database) -> None:
    from agentworks.db import InitStatus

    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    # The orchestrator's pre-gate _guard_vm_status refuses VMs that
    # never finished initializing, so the seeded row must be COMPLETE.
    db.update_vm_init_status("box", InitStatus.COMPLETE)


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _stop_the_vm(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    from agentworks.db import VMStatus as _VMStatus

    _reachable(monkeypatch, False)
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row: events.append("status") or _VMStatus.STOPPED,
    )
    monkeypatch.setattr(
        ProxmoxPlatform, "start", lambda self, row: events.append("start")
    )
    monkeypatch.setattr(
        vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale")
    )


# -- the derived graph --------------------------------------------------------


def test_create_graph_derives_from_row_and_pending_name(
    db: Database, make_config  # noqa: ANN001
) -> None:
    """The pending workspace's graph: its only edge is the VM (whose
    row's site field is the vm-site edge), so the union is the site's
    config secret alone. The workspace template's env-block secret
    reference is a runtime input and stays OUT of the union (hermetic
    provisioning)."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node
    from agentworks.workspaces.nodes import pending_workspace_node

    config = make_config(WORKSPACE_ENV_SECTION)
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    resolver = Resolver(config, registry)

    vm_node = live_vm_node(db, config, registry, vm, resolver)
    pending = pending_workspace_node(db, config, "ws1", vm_node, None)
    nodes = walk(pending)

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box", "workspace/ws1"]
    assert secret_union(nodes) == ("proxmox-token",)


# -- gate-prompt parity (the per-command carry) -------------------------------


def test_create_stopped_vm_gate_resolves_once_and_seeds_the_boundary(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """workspace create on a stopped VM: the gate's just-in-time token
    resolve is the ONLY backend pass (the union is fully seeded by it,
    so the boundary contributes no pass of its own), nothing resolves
    twice or after, and the command completes. No phase banners: the
    imperative command never framed, and the realize body never
    frames."""
    config = make_config()
    _seed_vm(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    workspace_manager.create_workspace(db, config, name="ws1", vm_name="box")

    assert resolve_counter == [["proxmox-token"]]
    assert events == ["status", "start", "tailscale"]  # the gate ran
    assert mutation["ws_name"] == "ws1"
    row = db.get_workspace("ws1")
    assert row is not None and row.workspace_path == "/srv/ws1"
    # The final info prints exactly once, from the realize body.
    assert sum("Workspace 'ws1' created" in m for m in captured_output.info) == 1
    assert not any(m.startswith("=== ") for m in captured_output.info)


def test_create_reachable_vm_fast_path_costs_no_gate_resolve(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The gate's fast path on a reachable VM: no just-in-time resolve
    at all, so the command's whole union rides ONE boundary burst."""
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)

    workspace_manager.create_workspace(db, config, name="ws1", vm_name="box")

    assert resolve_counter == [["proxmox-token"]]
    assert db.get_workspace("ws1") is not None


def test_create_bad_template_bails_before_any_prompt_or_start(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The bail-early precedence, pinned: cheap validation (an unknown
    template) fails BEFORE the gate and before any secret is touched,
    with zero resolve calls and zero gate events, even on a stopped
    VM. Validation relocated behind the gate would trip this."""
    config = make_config()
    _seed_vm(db)
    events: list[str] = []
    _stop_the_vm(monkeypatch, events)

    # The raw ValueError is the imperative command's pre-existing error
    # shape for an unknown template (asserted as-is: this test pins the
    # ORDER, not the shape).
    with pytest.raises(ValueError, match="nope"):
        workspace_manager.create_workspace(
            db, config, name="ws1", vm_name="box", template_name="nope"
        )

    assert resolve_counter == []  # no prompt, no backend pass
    assert events == []  # no status probe, no start
    assert db.get_workspace("ws1") is None


def test_create_never_resolves_the_template_env_secret(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    mutation: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """End-to-end hermeticity: driving the real command against an
    env-bearing workspace template never resolves the env secret (a
    runtime input, delivered where sessions run), on top of the
    union-level pin above."""
    config = make_config(WORKSPACE_ENV_SECTION)
    _seed_vm(db)
    _reachable(monkeypatch, True)

    workspace_manager.create_workspace(db, config, name="ws1", vm_name="box")

    assert resolve_counter == [["proxmox-token"]]
    assert all(
        "ws-env-secret" not in burst for burst in resolve_counter
    ), "the template env secret must never join a provisioning pass"
    assert db.get_workspace("ws1") is not None


# -- failure parity -----------------------------------------------------------


def test_create_mutation_failure_cleans_up_and_leaves_no_row(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The imperative rollback window, reproduced: a failure after the
    on-VM directory exists deletes it (the body's own cleanup), wraps
    in the same ExternalError, and no DB row ever exists; nothing else
    is unwound (there is nothing realized to unwind)."""
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch, True)
    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.create_vm_workspace",
        lambda vm, config_, ws_name, template, *, logger=None: f"/srv/{ws_name}",
    )

    def _boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("ssh exploded")

    # Raise AFTER the directory exists (from the VS Code stub step) so
    # the cleanup path has partial state to remove.
    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.generate_vscode_workspace", _boom
    )
    deletes: list[str] = []
    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.delete_vm_workspace",
        lambda vm, config_, ws_name, path, *, logger=None: deletes.append(path),
    )

    with pytest.raises(ExternalError, match="creating workspace: ssh exploded"):
        workspace_manager.create_workspace(db, config, name="ws1", vm_name="box")

    assert deletes == ["/srv/ws1"]  # the body's partial-state cleanup ran
    assert db.get_workspace("ws1") is None


# -- the operation scope reaches readiness ------------------------------------


def test_workspace_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    mutation: dict[str, Any],
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

    workspace_manager.create_workspace(db, config, name="ws1", vm_name="box")

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.WORKSPACE
    assert scope.vm == "box" and scope.workspace == "ws1"
    assert scope.agent is None and scope.session is None
