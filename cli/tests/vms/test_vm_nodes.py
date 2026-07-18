"""The live VM node's gate surface: parity with the imperative oracle
(``vms.manager.ensure_active`` / ``keep_active``, whose semantics these
tests mirror case for case; see ``test_ensure_active.py``), driven
through the orchestration gate helper the migrated command uses.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.orchestration.activation import activation_gate, ensure_active
from agentworks.vms import manager as vm_manager
from agentworks.vms.nodes import LiveVMNode, VMSiteNode

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.resources.registry import Registry


class _GatePlatform:
    """Recording platform double (same shape as the ensure_active
    oracle tests')."""

    name = "stub"

    def __init__(self, status: VMStatus = VMStatus.RUNNING) -> None:
        self._status = status
        self.status_calls = 0
        self.start_calls = 0
        self.holds = 0
        self.events: list[str] = []

    def status(self, vm: VMRow, ctx: object) -> VMStatus:
        self.status_calls += 1
        self.events.append("status")
        return self._status

    def start(self, vm: VMRow, ctx: object) -> None:
        self.start_calls += 1
        self.events.append("start")

    @contextlib.contextmanager
    def vm_active(
        self, vm: VMRow, *, config: object | None = None
    ) -> Iterator[None]:
        self.holds += 1
        self.events.append("hold-open")
        try:
            yield
        finally:
            self.events.append("hold-close")


def _node(
    db: Database, platform: _GatePlatform, vm: VMRow
) -> tuple[LiveVMNode, VMSiteNode]:
    site = VMSiteNode(
        "stub", cast("VMPlatform", platform), (), cast("Registry", object())
    )
    node = LiveVMNode(
        db, cast("Config", object()), cast("Registry", object()), vm, site
    )
    return node, site


def _seed(db: Database, *, tailscale: str | None = "100.64.0.9") -> VMRow:
    db.insert_vm("gvm", site="stub", hostname="gvm")
    if tailscale:
        db.update_vm_tailscale("gvm", tailscale)
    vm = db.get_vm("gvm")
    assert vm is not None
    return vm


def _no_resolve(name: str) -> str:
    raise AssertionError(f"gate resolved '{name}' unexpectedly")


def test_fast_path_skips_status_and_secrets(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oracle: a reachable Tailscale host short-circuits before any
    backend round trip; gate addition: before any secret, too."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    platform = _GatePlatform()
    node, _ = _node(db, platform, vm)

    assert ensure_active(node, _no_resolve) == {}
    assert platform.status_calls == 0
    assert platform.start_calls == 0


def test_auto_resume_starts_and_holds_through_tailscale(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    """Oracle: STOPPED without operator intent starts, then verifies
    Tailscale inside the platform hold."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)
    monkeypatch.setattr(
        vm_manager,
        "_ensure_tailscale",
        lambda *a, **k: platform.events.append("tailscale"),
    )

    ensure_active(node, _no_resolve)
    assert platform.events == [
        "status",
        "start",
        "hold-open",
        "tailscale",
        "hold-close",
    ]


def test_manually_stopped_raises_and_skips_the_ping(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oracle: the refusal names the operator's own action, carries the
    explicit-start hint, and the row's flag skips the reachability
    probe (pinging a stopped VM would burn the timeout to reach the
    refusal)."""
    _seed(db)
    db.set_operator_stopped("gvm", True)
    vm = db.get_vm("gvm")
    assert vm is not None

    def _no_ping(host: str) -> bool:
        raise AssertionError("reachability probed for a manually stopped VM")

    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", _no_ping)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)

    with pytest.raises(StateError, match="manually stopped") as exc:
        ensure_active(node, _no_resolve)
    assert "not be auto-started" in str(exc.value)
    assert "agw vm start gvm" in (exc.value.hint or "")
    assert platform.start_calls == 0


def test_manually_stopped_but_running_out_of_band_proceeds(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oracle: the flag is intent, not observed state; RUNNING proceeds
    without a start and without raising."""
    _seed(db)
    db.set_operator_stopped("gvm", True)
    vm = db.get_vm("gvm")
    assert vm is not None
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.RUNNING)
    node, _ = _node(db, platform, vm)

    ensure_active(node, _no_resolve)
    assert platform.start_calls == 0


def test_flag_is_reread_before_auto_start(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oracle: a concurrent `vm stop` between the row load and the gate
    must not be auto-undone (the re-read race guard)."""
    vm = _seed(db)  # loaded with operator_stopped=False
    db.set_operator_stopped("gvm", True)  # another terminal stops it
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)

    with pytest.raises(StateError, match="stopped"):
        ensure_active(node, _no_resolve)
    assert platform.start_calls == 0


def test_concurrent_start_clears_the_flag_and_resumes(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    """Oracle: the mirror race; a `vm start` in another terminal
    cleared the flag after the row load, so the gate auto-resumes."""
    _seed(db)
    db.set_operator_stopped("gvm", True)
    vm = db.get_vm("gvm")  # loaded with operator_stopped=True
    assert vm is not None
    db.set_operator_stopped("gvm", False)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: None)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)

    ensure_active(node, _no_resolve)
    assert platform.start_calls == 1


def test_deallocated_auto_resumes_like_stopped(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: None)
    platform = _GatePlatform(status=VMStatus.DEALLOCATED)
    node, _ = _node(db, platform, vm)

    ensure_active(node, _no_resolve)
    assert platform.start_calls == 1


def test_unknown_status_proceeds_without_start(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oracle: a transient status failure must not trigger a spurious
    start; the real op surfaces the real error."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.UNKNOWN)
    node, _ = _node(db, platform, vm)

    ensure_active(node, _no_resolve)
    assert platform.start_calls == 0


def test_gate_span_holds_through_the_command_body(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``activation_gate`` over the node is ``keep_active``'s shape:
    converge, then hold for the body's duration."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    platform = _GatePlatform()
    node, _ = _node(db, platform, vm)

    with activation_gate(node, _no_resolve):
        platform.events.append("body")
    assert platform.events == ["hold-open", "body", "hold-close"]


def test_rejoin_auth_key_reads_lazily_through_the_gate_reader(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    """The repair path: the node hands ``_ensure_tailscale`` an
    ``auth_key_source`` backed by the gate's lazy reader, so the key
    resolves only when the rejoin actually needs it and the resolved
    value lands in the gate's returned seed."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)
    monkeypatch.setattr(
        node, "repair_secret_refs", lambda: ("tailscale-auth-key",)
    )
    captured: dict[str, Callable[[], str] | None] = {}

    def _capture(*a: object, **k: object) -> None:
        captured["source"] = k.get("auth_key_source")  # type: ignore[assignment]

    monkeypatch.setattr(vm_manager, "_ensure_tailscale", _capture)
    resolved: list[str] = []

    def _resolve(name: str) -> str:
        resolved.append(name)
        return "ts-key"

    values = ensure_active(node, _resolve)
    assert resolved == []  # nothing resolved eagerly
    source = captured["source"]
    assert source is not None
    assert source() == "ts-key"  # the rejoin's first need resolves it
    assert resolved == ["tailscale-auth-key"]
    assert values == {"tailscale-auth-key": "ts-key"}


# -- the vm-template node ----------------------------------------------------


def test_template_node_declares_only_the_tailscale_key() -> None:
    """Hermetic provisioning: the template's env-block secrets are
    runtime inputs, so they must NOT fold into the node's secret_refs
    (they would otherwise join a provisioning command's boundary
    resolve and prompt)."""
    from agentworks.env.entry import EnvEntry
    from agentworks.vms.nodes import vm_template_node
    from agentworks.vms.templates import ResolvedVMTemplate

    tmpl = ResolvedVMTemplate(
        name="default",
        env={"API_KEY": EnvEntry(key="API_KEY", secret="api-key")},
    )
    node = vm_template_node(tmpl, cast("Registry", object()))
    assert node.key == "vm-template/default"
    assert node.secret_refs() == ("tailscale-auth-key",)
    assert node.deps() == ()


def test_template_node_preflight_predicts_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The relocated readiness check predicts CENTRALLY over the key's
    declaration (the held-resolver prediction seam is closed): with the
    env-var backend alone, a set variable predicts resolvable and an
    unset one raises the delegate's old typed error."""
    from agentworks.bootstrap import build_registry
    from agentworks.capabilities.base import RunContext
    from agentworks.errors import ConfigError
    from agentworks.vms.nodes import vm_template_node
    from agentworks.vms.templates import ResolvedVMTemplate
    from tests.orchestrated_fixtures import write_operator_config

    config = write_operator_config(
        tmp_path, '[secret_config]\nbackends = ["env-var"]\n'
    )
    registry = build_registry(config)
    node = vm_template_node(ResolvedVMTemplate(name="default"), registry)

    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey")
    node.preflight(RunContext(config=config))  # no error

    monkeypatch.delenv("AW_SECRET_TAILSCALE_AUTH_KEY")
    with pytest.raises(ConfigError, match="not resolvable"):
        node.preflight(RunContext(config=config))


# -- the pending VM node -----------------------------------------------------


def _pending(db: Database):
    from agentworks.vms.nodes import pending_vm_node, vm_template_node
    from agentworks.vms.templates import ResolvedVMTemplate

    template = vm_template_node(
        ResolvedVMTemplate(name="default"), cast("Registry", object())
    )
    site = VMSiteNode(
        "stub", cast("VMPlatform", _GatePlatform()), (), cast("Registry", object())
    )
    return pending_vm_node(db, "nvm", template, site, ()), template, site


def test_pending_vm_node_shape_and_edges(db: Database) -> None:
    from agentworks.orchestration.node import CreatableNode, Node

    node, template, site = _pending(db)
    assert node.key == "vm/nvm"
    assert isinstance(node, Node)
    assert isinstance(node, CreatableNode)
    # Edges attached at construction, same objects the orchestrator
    # planned with (one object per node).
    assert node.deps() == (template, site)
    assert not node.realized


def test_pending_vm_realization_is_one_way(db: Database) -> None:
    node, _, _ = _pending(db)
    node.mark_realized()
    assert node.realized
    with pytest.raises(StateError, match="one-way"):
        node.mark_realized()


def test_pending_vm_teardown_deletes_the_row(db: Database) -> None:
    """The relocated rollback body: exactly today's create_vm rollback
    (delete the DB record), now the node's own teardown op."""
    db.insert_vm("nvm", site="stub", hostname="nvm")
    node, _, _ = _pending(db)
    node.mark_realized()
    node.teardown()
    assert db.get_vm("nvm") is None
