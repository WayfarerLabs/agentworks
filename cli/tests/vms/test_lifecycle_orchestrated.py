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

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.db import ProvisioningStatus, VMStatus
from agentworks.execution import trust
from agentworks.execution.errors import TrustMissing
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from agentworks.vms.manager.tailscale import _await_tailnet
from tests.conftest import seed_vm_trust

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.capabilities.base import OperationScope, RunContext
    from agentworks.db import Database


def _seed_vm(db: Database, *, operator_stopped: bool = False) -> None:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
    seed_vm_trust("box")
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
        lambda self, row, ctx: events.append("status") or status,
    )
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: events.append("start"))
    monkeypatch.setattr(ProxmoxPlatform, "stop", lambda self, row, ctx: events.append("stop"))
    monkeypatch.setattr(vm_manager, "_await_tailnet", lambda *a, **k: events.append("tailscale"))
    return events


# -- the derived graph --------------------------------------------------------


def test_lifecycle_graph_derives_from_row(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    """The lifecycle commands' shared graph (start / stop / delete all
    build it through ``_live_vm_boundary``): the live VM whose row's
    site field is the vm-site edge, nothing else, so the union is the
    site's config secret alone. Each command's boundary-burst test
    below pins that union per command."""
    from agentworks.bootstrap import build_registry
    from agentworks.orchestration.secrets import secret_union
    from agentworks.orchestration.walk import walk
    from agentworks.vms.nodes import live_vm_node

    config = make_config()
    _seed_vm(db)
    vm = db.get_vm("box")
    assert vm is not None
    registry = build_registry(config)

    nodes = walk(live_vm_node(db, config, registry, vm))

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
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    _seed_vm(db, operator_stopped=True)
    events = _fake_power(monkeypatch, VMStatus.STOPPED)
    record_vm_started = db.record_vm_started

    def _record_started(name: str) -> None:
        events.append("record-start")
        record_vm_started(name)

    monkeypatch.setattr(db, "record_vm_started", _record_started)

    vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert events == ["status", "start", "record-start", "tailscale"]
    row = db.get_vm("box")
    assert row is not None and row.operator_stopped is False
    assert row.last_started_at is not None
    assert any("VM 'box' is ready" in m for m in captured_output.info)
    assert not any("already running" in m for m in captured_output.info)


class _ReachedTrust(Exception):
    """Recover got as far as re-establishing the missing record."""


def test_a_stopped_vm_without_a_host_key_record_starts_then_fails_naming_recover_which_can_then_run(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """A VM from before the host-key store, stopped while idle: the start boots it, and the real wait's
    first probe raises TrustMissing (whose hint names recover) before anything spawns. Recover, which
    refuses a stopped VM, then runs on the now running VM."""
    import agentworks.execution as execution
    from agentworks.execution.client import SSHClient

    config = make_config()
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    _seed_vm(db)
    trust.remove("box")
    events = _fake_power(monkeypatch, VMStatus.STOPPED)
    monkeypatch.setattr(vm_manager, "_await_tailnet", _await_tailnet)
    monkeypatch.setattr(execution, "resolve_ssh", lambda: SSHClient("/usr/bin/ssh", (9, 2)))

    with pytest.raises(TrustMissing) as caught:
        vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["status", "start"]
    # The start adds to the record's own hint, since the VM it booted stays up.
    assert caught.value.hint != TrustMissing("box").hint
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.RUNNING)
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)

    def _reached(*_args: object, **_kwargs: object) -> None:
        raise _ReachedTrust

    monkeypatch.setattr("agentworks.vms.manager.recover._reestablish_trust", _reached)
    with pytest.raises(_ReachedTrust):
        vm_manager.recover_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)


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
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    _seed_vm(db, operator_stopped=True)
    original_start = "2026-01-01T00:00:00Z"
    db._conn.execute("UPDATE vms SET last_started_at = ? WHERE name = ?", (original_start, "box"))
    db._conn.commit()
    events = _fake_power(monkeypatch, VMStatus.RUNNING)

    vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert resolve_counter == [["proxmox-token"]]
    assert events == ["status", "tailscale"]
    row = db.get_vm("box")
    assert row is not None and row.operator_stopped is False
    assert row.last_started_at == original_start
    assert any("VM 'box' is already running" in m for m in captured_output.info)
    assert not any("is ready" in m for m in captured_output.info)


def test_start_with_unknown_status_clears_stale_start_observation(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config()
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    _seed_vm(db)
    original_start = "2026-01-01T00:00:00Z"
    db._conn.execute("UPDATE vms SET last_started_at = ? WHERE name = ?", (original_start, "box"))
    db._conn.commit()
    events = _fake_power(monkeypatch, VMStatus.UNKNOWN)

    vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["status", "start", "tailscale"]
    row = db.get_vm("box")
    assert row is not None and row.last_started_at is None


def _patch_recording_start_hold(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.STOPPED)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: None)

    @contextlib.contextmanager
    def _hold(self: object, row: object, *, config: object) -> Iterator[None]:
        events.append("hold-enter")
        try:
            yield
        finally:
            events.append("hold-exit")

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _hold)


@pytest.mark.parametrize("answers", [True, False], ids=["answers", "silent"])
def test_start_waits_inside_the_hold_and_resolves_no_tailscale_key(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    answers: bool,
) -> None:
    """The start's one wait runs inside the hold. A VM that does not
    answer fails naming recover, having resolved only the site's secret
    and dispatched nothing to its root channel, whatever key the source
    chain holds."""
    from agentworks.execution import reachability
    from agentworks.vms.manager.tailscale import VMNotAnswering

    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    config = make_config()
    _seed_vm(db)
    events: list[str] = []
    _patch_recording_start_hold(monkeypatch, events)
    monkeypatch.setattr(
        ProxmoxPlatform, "run_as_root", lambda *a, **k: pytest.fail("a script was dispatched to the VM")
    )

    def _wait(access: object, *, wording: reachability.Wording) -> bool:
        events.append(f"wait:{wording.name}")
        return answers

    monkeypatch.setattr(reachability, "wait", _wait)

    if answers:
        vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)
    else:
        with pytest.raises(VMNotAnswering):
            vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    assert events == ["hold-enter", "wait:RECONNECT", "hold-exit"]
    assert resolve_counter == [["proxmox-token"]]


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

    vm_manager.stop_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

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

    vm_manager.stop_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    row = db.get_vm("box")
    assert row is not None and row.operator_stopped is True
    assert events == ["status"]  # short-circuited, no stop op
    assert resolve_counter == [["proxmox-token"]]
    # The boundary resolve emits a "Resolved ..." info line of its own; the
    # stop message is the remaining info line.
    (message,) = [m for m in captured_output.info if not m.startswith("Resolved ")]
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

    vm_manager.stop_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    (message,) = [m for m in captured_output.info if not m.startswith("Resolved ")]
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
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    _seed_vm(db)
    _fake_power(monkeypatch, VMStatus.RUNNING)
    scopes: list[OperationScope | None] = []
    real = ProxmoxPlatform.preflight

    def _recording(self: ProxmoxPlatform, ctx: RunContext) -> None:
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)

    vm_manager.start_vm(db, config, "box", interaction=TtyInteractionPolicy.REFUSE)

    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"
    assert scope.workspace is None and scope.agent is None and scope.session is None
