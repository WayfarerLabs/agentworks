"""The last straggler commands through the orchestrated model:
``describe_vm``, ``rekey_vm``, ``port_forward_vm``, and ``backup_vm``
(the seam that finished the still-open caller catalog).

Per-command shape: describe composes without gating
(``_live_vm_boundary``: one boundary burst, a status read as its op,
NEVER a gate or a start); rekey composes the vm-template node beside
the live VM (the new auth key IS its planned op) and gates AFTER the
boundary, exactly where HEAD held ``keep_active``; port-forward and
backup are gated commands (``gated_vm_boundary``) with their
validation pre-gate.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend power ops, the reachability probe, and the
SSH/subprocess transports are the fakes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.db import VMStatus
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database


def _seed_vm(db: Database, *, operator_stopped: bool = False) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    if operator_stopped:
        db.set_operator_stopped("box", True)


def _reachable(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: value)


def _fake_status(
    monkeypatch: pytest.MonkeyPatch, status: VMStatus
) -> list[str]:
    """Fake the platform's backend status read (recording the op order);
    ``start`` records too, so never-gates pins can assert its absence."""
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
        vm_manager, "_ensure_tailscale", lambda *a, **k: events.append("tailscale")
    )
    return events


# == describe_vm: composition without a gate ==================================


@pytest.fixture
def _quiet_backend_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub describe's non-status backend reads (the display name is a
    backend API render; the live-resource query SSHes to the VM)."""
    monkeypatch.setattr(
        ProxmoxPlatform, "display_backend_name", lambda self, row: "vmid 100"
    )
    monkeypatch.setattr(
        vm_manager, "_query_live_resources", lambda vm, config: None
    )


def test_describe_running_vm_is_one_boundary_burst_and_reads_only(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    _quiet_backend_reads: None,
) -> None:
    """The no-gate mirror of the tracer's parity shape: exactly ONE
    boundary burst covering the union, then the status read (describe's
    op), and nothing else; the report renders the observed status."""
    config = make_config()
    _seed_vm(db)
    events = _fake_status(monkeypatch, VMStatus.RUNNING)

    vm_manager.describe_vm(db, config, "box")

    assert resolve_counter == [["proxmox-token"]]
    assert events == ["status"]
    assert any("Status:         running" in m for m in captured_output.info)


def test_describe_operator_stopped_vm_never_gates(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    _quiet_backend_reads: None,
) -> None:
    """The never-gates pin: on an operator-stopped VM describe behaves
    exactly as HEAD: one status read, NO start, the "(manual)" label,
    and the live SSH read skipped (the stopped-VM short-circuit)."""
    config = make_config()
    _seed_vm(db, operator_stopped=True)
    events = _fake_status(monkeypatch, VMStatus.STOPPED)

    def _no_live(vm: object, config: object) -> None:
        raise AssertionError("live SSH read must be skipped on a stopped VM")

    monkeypatch.setattr(vm_manager, "_query_live_resources", _no_live)

    vm_manager.describe_vm(db, config, "box")

    assert resolve_counter == [["proxmox-token"]]
    assert events == ["status"]  # never "start"
    assert any("stopped (manual)" in m for m in captured_output.info)


def test_describe_scope_reaches_node_readiness(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
    _quiet_backend_reads: None,
) -> None:
    from agentworks.capabilities.base import ScopeLevel

    config = make_config()
    _seed_vm(db)
    _fake_status(monkeypatch, VMStatus.RUNNING)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    vm_manager.describe_vm(db, config, "box")

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"
