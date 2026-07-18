"""``vm start`` / ``vm stop`` through the orchestrated model: the
lifecycle commands' shared derived graph, the boundary-burst parity
(these commands open NO activation gate: the power op IS the
operation), the operator-stopped flag semantics end to end, and the VM
scope reaching node readiness.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops and the Tailscale verification are
the fakes. ``vm delete`` shares the same composition root
(``_live_vm_boundary``); its failure discipline and its no-gate /
boundary-burst pins live in ``test_delete_vm_gating.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.config import load_config
from agentworks.db import VMStatus
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database

PROXMOX_SECTION = """
[proxmox]
api_url = "https://pve:8006"
node = "pve1"
token_id = "agw@pam!agw"
template_vmid = 9000
"""


@pytest.fixture
def make_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")
    monkeypatch.setenv("AW_SECRET_PROXMOX_TOKEN", "pve-token")

    def _make(extra: str = ""):  # noqa: ANN202
        path = tmp_path / "config.toml"
        path.write_text(
            f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n'
            + PROXMOX_SECTION
            + extra
        )
        return load_config(path, warn_issues=False, warn_deprecations=False)

    return _make


@pytest.fixture
def resolve_counter(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every backend-loop pass (the prompt-session oracle)."""
    from agentworks.secrets import resolve as secrets_resolve

    calls: list[list[str]] = []
    real = secrets_resolve.resolve_secrets

    def _counting(secrets: list[object], *args: object, **kwargs: object) -> dict[str, str]:
        calls.append([getattr(s, "name", str(s)) for s in secrets])
        return real(secrets, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secrets_resolve, "resolve_secrets", _counting)
    return calls


def _seed_vm(db: Database, *, operator_stopped: bool = False) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    if operator_stopped:
        db.set_operator_stopped("box", True)


def _fake_power(monkeypatch: pytest.MonkeyPatch, status: VMStatus) -> list[str]:
    """Fake the platform's backend power ops (recording the op order)
    and the Tailscale verification; everything upstream of them
    (registry, resolver, preflight, resolve) runs for real."""
    events: list[str] = []
    monkeypatch.setattr(
        ProxmoxPlatform,
        "status",
        lambda self, row: events.append("status") or status,
    )
    monkeypatch.setattr(
        ProxmoxPlatform, "start", lambda self, row: events.append("start")
    )
    monkeypatch.setattr(
        ProxmoxPlatform, "stop", lambda self, row: events.append("stop")
    )
    monkeypatch.setattr(
        vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale")
    )
    return events


# -- the derived graph --------------------------------------------------------


def test_lifecycle_graph_derives_from_row(
    db: Database, make_config  # noqa: ANN001
) -> None:
    """The lifecycle commands' shared graph (start / stop / delete all
    build it through ``_live_vm_boundary``): the live VM whose row's
    site field is the vm-site edge, nothing else, so the union is the
    site's config secret alone. Each command's boundary-burst test
    below pins that union per command."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.secrets.resolver import Resolver
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)
    resolver = Resolver(config, registry)

    nodes = walk(live_vm_node(db, config, registry, vm, resolver))

    assert [n.key for n in nodes] == ["vm-site/proxmox", "vm/box"]
    assert secret_union(nodes) == ("proxmox-token",)


# -- vm start: boundary burst, flag clear, short-circuits ---------------------


def test_start_stopped_vm_resolves_once_starts_and_clears_flag(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The mirror of the tracer's gate-prompt parity, no-gate shape:
    exactly ONE boundary burst covering the union, nothing resolved
    twice, nothing after; the start drives through the node's held
    platform; the operator-stopped flag is cleared (an explicit start
    is operator intent)."""
    config = make_config()
    _seed_vm(db, operator_stopped=True)
    events = _fake_power(monkeypatch, VMStatus.STOPPED)

    vm_manager.start_vm(db, config, "box")

    assert resolve_counter == [["proxmox-token"]]
    assert events == ["status", "start", "tailscale"]
    row = db.get_vm("box")
    assert row is not None and row.operator_stopped is False
    assert any("VM 'box' is ready" in m for m in captured_output.info)
    assert not any("already running" in m for m in captured_output.info)


def test_start_running_vm_short_circuits_but_still_clears_flag(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """An already-running VM: no start op, the 'already running' info
    (and no 'is ready' noise), Tailscale still verified, and the flag
    still cleared; the boundary still costs exactly one burst."""
    config = make_config()
    _seed_vm(db, operator_stopped=True)
    events = _fake_power(monkeypatch, VMStatus.RUNNING)

    vm_manager.start_vm(db, config, "box")

    assert resolve_counter == [["proxmox-token"]]
    assert events == ["status", "tailscale"]
    row = db.get_vm("box")
    assert row is not None and row.operator_stopped is False
    assert any("VM 'box' is already running" in m for m in captured_output.info)
    assert not any("is ready" in m for m in captured_output.info)


# -- vm stop: boundary burst, flag set, short-circuits ------------------------


def test_stop_running_vm_resolves_once_stops_and_sets_flag(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    config = make_config()
    _seed_vm(db)
    events = _fake_power(monkeypatch, VMStatus.RUNNING)

    vm_manager.stop_vm(db, config, "box")

    assert resolve_counter == [["proxmox-token"]]
    assert events == ["status", "stop"]
    row = db.get_vm("box")
    assert row is not None and row.operator_stopped is True
    assert any(m == "VM 'box' stopped" for m in captured_output.info)


def test_stop_sets_flag_before_already_stopped_shortcut(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Stopping an idle-stopped VM still records the intent, and the
    message says so instead of the misleading bare 'already stopped'
    (the command DID change something: auto-start is now off)."""
    config = make_config()
    _seed_vm(db)
    events = _fake_power(monkeypatch, VMStatus.STOPPED)

    vm_manager.stop_vm(db, config, "box")

    row = db.get_vm("box")
    assert row is not None and row.operator_stopped is True
    assert events == ["status"]  # short-circuited, no stop op
    assert resolve_counter == [["proxmox-token"]]
    (message,) = captured_output.info
    assert "stopped on its own" in message
    assert "will not be auto-started" in message


def test_stop_of_a_manually_stopped_vm_is_a_true_noop(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """Only when the intent was ALREADY recorded does 'already' apply,
    and it names the manual state."""
    config = make_config()
    _seed_vm(db, operator_stopped=True)
    _fake_power(monkeypatch, VMStatus.STOPPED)

    vm_manager.stop_vm(db, config, "box")

    (message,) = captured_output.info
    assert message == "VM 'box' is already manually stopped"


# -- the operation scope reaches readiness ------------------------------------


def test_vm_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed_vm(db)
    _fake_power(monkeypatch, VMStatus.RUNNING)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    vm_manager.start_vm(db, config, "box")

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"
    assert scope.workspace is None and scope.agent is None and scope.session is None
