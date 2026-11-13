"""The live VM node's gate surface, driven through the orchestration
gate helper every command uses. These cases mirror the power-state
semantics the retired imperative ``vms.manager.ensure_active`` /
``keep_active`` pair once carried (fast-path skip, auto-resume,
operator-stopped refusal and its race re-read, out-of-band running,
deallocated, unknown, and the held span).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.db import DesiredRuntimeState as Target
from agentworks.db import VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import ConfigError, StateError
from agentworks.execution import trust
from agentworks.execution.errors import TrustMissing
from agentworks.orchestration.activation import activation_gate, ensure_active
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from agentworks.vms.nodes import LiveVMNode, VMSiteNode
from tests.conftest import seed_vm_trust

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from agentworks.capabilities.base import SecretReader
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.resources.registry import Registry


class _GatePlatform:
    """Recording platform double for the gate tests."""

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
    def vm_active(self, vm: VMRow, *, config: object | None = None) -> Iterator[None]:
        self.holds += 1
        self.events.append("hold-open")
        try:
            yield
        finally:
            self.events.append("hold-close")


def _node(db: Database, platform: _GatePlatform, vm: VMRow) -> tuple[LiveVMNode, VMSiteNode]:
    site = VMSiteNode("stub", cast("VMPlatform", platform), (), cast("Registry", object()))
    node = LiveVMNode(db, cast("Config", object()), cast("Registry", object()), vm, site)
    return node, site


def _seed(db: Database, *, tailscale: str | None = "100.64.0.9") -> VMRow:
    db.insert_vm("gvm", site="stub", hostname="gvm")
    if tailscale:
        db.update_vm_tailscale("gvm", tailscale)
    seed_vm_trust("gvm")
    vm = db.get_vm("gvm")
    assert vm is not None
    return vm


def _no_resolve(name: str) -> str:
    raise AssertionError(f"gate resolved '{name}' unexpectedly")


def test_fast_path_skips_status_and_secrets(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
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
    """Oracle: STOPPED without operator intent starts, then waits for
    the VM to answer inside the platform hold."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)
    monkeypatch.setattr(vm_manager, "_await_tailnet", lambda *a, **k: platform.events.append("tailscale"))

    ensure_active(node, lambda name: "ts-key")
    assert platform.events == [
        "status",
        "status",
        "start",
        "hold-open",
        "tailscale",
        "hold-close",
    ]


def test_auto_start_without_a_host_key_record_raises_before_starting_or_resolving(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A VM with no record could only fail its wait after the start."""
    vm = _seed(db)
    trust.remove("gvm")
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)
    resolved: list[str] = []

    def _resolve(name: str) -> str:
        resolved.append(name)
        return "ts-key"

    with pytest.raises(TrustMissing):
        ensure_active(node, _resolve)

    assert platform.start_calls == 0
    assert platform.holds == 0
    assert resolved == []


def test_an_auto_started_vm_that_does_not_answer_names_recover_and_resolves_nothing(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object, vm_access_recorder: object
) -> None:
    """The wait runs inside the hold, as a reconnect; a VM that does not
    answer raises naming recover, and the gate resolves nothing past its
    gate names."""
    from agentworks.execution import reachability
    from agentworks.vms.manager.tailscale import VMNotAnswering

    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)

    def _wait(access: object, *, wording: reachability.Wording) -> bool:
        platform.events.append(f"wait:{wording.name}")
        return False

    monkeypatch.setattr(reachability, "wait", _wait)

    with pytest.raises(VMNotAnswering):
        ensure_active(node, _no_resolve)

    assert platform.events == ["status", "status", "start", "hold-open", "wait:RECONNECT", "hold-close"]


def test_manually_stopped_raises_and_skips_the_ping(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Oracle: the refusal names the operator's own action, carries the
    explicit-start hint, and the row's flag skips the reachability
    probe (pinging a stopped VM would burn the timeout to reach the
    refusal)."""
    _seed(db)
    db.set_vm_desired_runtime_state(
        "gvm", Target.STOPPED, expected_vm_uuid=next(vm.vm_uuid for vm in db.list_vms() if vm.name == "gvm")
    )
    vm = db.get_vm("gvm")
    assert vm is not None

    def _no_ping(host: str) -> bool:
        raise AssertionError("reachability probed for a manually stopped VM")

    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", _no_ping)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)

    with pytest.raises(StateError):
        ensure_active(node, _no_resolve)
    assert platform.start_calls == 0


def test_manually_stopped_but_running_out_of_band_proceeds(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Oracle: the flag is intent, not observed state; RUNNING proceeds
    without a start and without raising."""
    _seed(db)
    db.set_vm_desired_runtime_state(
        "gvm", Target.STOPPED, expected_vm_uuid=next(vm.vm_uuid for vm in db.list_vms() if vm.name == "gvm")
    )
    vm = db.get_vm("gvm")
    assert vm is not None
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.RUNNING)
    node, _ = _node(db, platform, vm)

    ensure_active(node, lambda name: "ts-key")
    assert platform.start_calls == 0


def test_flag_is_reread_before_auto_start(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Oracle: a concurrent `vm stop` between the row load and the gate
    must not be auto-undone (the re-read race guard)."""
    vm = _seed(db)  # loaded with operator_stopped=False
    db.set_vm_desired_runtime_state(
        "gvm", Target.STOPPED, expected_vm_uuid=next(vm.vm_uuid for vm in db.list_vms() if vm.name == "gvm")
    )  # another terminal stops it
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)

    with pytest.raises(StateError):
        ensure_active(node, _no_resolve)
    assert platform.start_calls == 0


def test_concurrent_start_clears_the_flag_and_resumes(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    """Oracle: the mirror race; a `vm start` in another terminal
    cleared the flag after the row load, so the gate auto-resumes."""
    _seed(db)
    db.set_vm_desired_runtime_state(
        "gvm", Target.STOPPED, expected_vm_uuid=next(vm.vm_uuid for vm in db.list_vms() if vm.name == "gvm")
    )
    vm = db.get_vm("gvm")  # loaded with operator_stopped=True
    assert vm is not None
    db.set_vm_desired_runtime_state(
        "gvm", Target.ON_DEMAND, expected_vm_uuid=next(vm.vm_uuid for vm in db.list_vms() if vm.name == "gvm")
    )
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    monkeypatch.setattr(vm_manager, "_await_tailnet", lambda *a, **k: None)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)

    ensure_active(node, lambda name: "ts-key")
    assert platform.start_calls == 1
    refreshed = db.get_vm("gvm")
    assert refreshed is not None and refreshed.last_started_at is not None


def test_deallocated_auto_resumes_like_stopped(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    monkeypatch.setattr(vm_manager, "_await_tailnet", lambda *a, **k: None)
    platform = _GatePlatform(status=VMStatus.DEALLOCATED)
    node, _ = _node(db, platform, vm)

    ensure_active(node, lambda name: "ts-key")
    assert platform.start_calls == 1


def test_unknown_status_proceeds_without_start(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Oracle: a transient status failure must not trigger a spurious
    start; the real op surfaces the real error."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.UNKNOWN)
    node, _ = _node(db, platform, vm)

    ensure_active(node, _no_resolve)
    assert platform.start_calls == 0


def test_gate_span_holds_through_the_command_body(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """``activation_gate`` over the node is ``keep_active``'s shape:
    converge, then hold for the body's duration."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: True)
    platform = _GatePlatform()
    node, _ = _node(db, platform, vm)

    with activation_gate(node, _no_resolve):
        platform.events.append("body")
    assert platform.events == ["hold-open", "body", "hold-close"]


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
        env={"API_KEY": EnvEntry({"secret": "api-key"})},
    )
    node = vm_template_node(tmpl)
    assert node.key == "vm-template/default"
    assert node.secret_refs() == ("tailscale-auth-key",)
    assert node.deps() == ()


def test_template_node_declares_the_key_and_the_sweep_predicts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auth key's resolvability check moved off this node and into
    the operation's preflight sweep, on the same rule the vm-site and
    git-credential nodes follow: a node declares, the operation predicts.

    The node's own preflight is now a no-op even when nothing can
    resolve the key (which is what keeps doctor-shaped callers honest),
    while the sweep over the same node still refuses, with the template
    named by the owner key and the key identified by its usage prose.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.readiness import preflight_all
    from agentworks.vms.nodes import vm_template_node
    from agentworks.vms.templates import ResolvedVMTemplate
    from tests.orchestrated_fixtures import write_operator_config

    config = write_operator_config(tmp_path, '[secret_config]\nsources = ["env-var"]\n')
    registry = build_registry(config)
    node = vm_template_node(ResolvedVMTemplate(name="default"))
    ctx = RunContext(config=config)

    # The node declares the key as the reference the vm-template kind
    # itself publishes, usage prose included.
    (ref,) = node.config_secret_refs()
    assert (ref.kind, ref.name, ref.usage) == ("secret", "tailscale-auth-key", "the Tailscale auth key")

    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey")
    preflight_all([node], ctx, registry=registry, interaction=TtyInteractionPolicy.REFUSE)  # resolvable: no error

    monkeypatch.delenv("AW_SECRET_TAILSCALE_AUTH_KEY")
    # Provider-aware preview checks the environment without returning its
    # value, so an ordinary miss fails preflight before mutation.
    node.preflight(ctx)
    with pytest.raises(ConfigError):
        preflight_all([node], ctx, registry=registry, interaction=TtyInteractionPolicy.REFUSE)


# -- the vm-site node's own preflight ----------------------------------------


def test_site_node_preflight_refuses_a_dangling_secret_reference(tmp_path: Path) -> None:
    """The site's preflight verifies its declarations are INTACT: a
    reference naming no registry row is a typed error, not a KeyError
    and not a silent pass on a synthesized declaration.

    This is the half that stayed the node's concern when resolvability
    prediction moved to the operation's preflight sweep. Whether the
    registry agrees with the site's own config is registry consistency;
    whether a declared secret would resolve is the operation's runtime
    world, and the site does not speak to it.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.capabilities.base import RunContext
    from agentworks.errors import ConfigError
    from agentworks.resources.reference import SecretReference
    from tests.orchestrated_fixtures import write_operator_config

    config = write_operator_config(tmp_path)
    registry = build_registry(config)
    dangling = SecretReference(
        name="never-declared",
        kind="secret",
        usage="the Proxmox API token",
        source=("vm-site", "stub"),
    )
    site = VMSiteNode("stub", cast("VMPlatform", _GatePlatform()), (dangling,), registry)

    with pytest.raises(ConfigError) as exc:
        site.preflight(RunContext(config=config))
    assert "vm-site/stub" in str(exc.value)
    assert "never-declared" in str(exc.value)


# -- the pending VM node -----------------------------------------------------


def _pending(db: Database):
    from agentworks.vms.nodes import pending_vm_node, vm_template_node
    from agentworks.vms.templates import ResolvedVMTemplate

    template = vm_template_node(ResolvedVMTemplate(name="default"))
    site = VMSiteNode("stub", cast("VMPlatform", _GatePlatform()), (), cast("Registry", object()))
    return pending_vm_node(db, "nvm", DebianRelease.TRIXIE, "nvm", template, site, ()), template, site


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
    with pytest.raises(StateError):
        node.mark_realized()
    assert node.realized


def test_pending_vm_teardown_deletes_the_row(db: Database) -> None:
    """The relocated rollback body: exactly today's create_vm rollback
    (delete the DB record), now the node's own teardown op."""
    db.insert_vm("nvm", site="stub", hostname="nvm")
    node, _, _ = _pending(db)
    node.mark_realized()
    node.teardown()
    assert db.get_vm("nvm") is None


@pytest.mark.windows
def test_auto_start_rechecks_power_with_family_guard(db, monkeypatch):
    from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard

    vm = _seed(db)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    monkeypatch.setattr(vm_manager, "_await_tailnet", lambda *a, **k: None)
    statuses = iter((VMStatus.STOPPED, VMStatus.RUNNING))

    def status(row, ctx):
        return next(statuses)

    monkeypatch.setattr(platform, "status", status)
    ensure_active(node, _no_resolve)
    assert platform.start_calls == 0
    with native_mutation_guard(db.path, vm.name), pytest.raises(NativeSetupBusyError):
        node.auto_start(cast("SecretReader", object()))
    assert platform.start_calls == 0


@pytest.mark.windows
def test_hold_admission_is_guarded_but_interactive_body_is_unlocked(db, monkeypatch):
    from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard

    vm = _seed(db)
    platform = _GatePlatform()
    node, _ = _node(db, platform, vm)

    @contextlib.contextmanager
    def delayed_attachment(row, *, config):
        with pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, vm.name):
            pytest.fail("stop admitted before keeper attachment")
        yield

    monkeypatch.setattr(platform, "vm_active", delayed_attachment)
    with node.hold_active(), native_mutation_guard(db.path, vm.name):
        pass


@pytest.mark.windows
def test_stopped_live_guest_access_skips_booting_hold_and_admission_can_be_busy(db, monkeypatch):
    from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard

    vm = _seed(db)
    db.set_vm_desired_runtime_state(vm.name, Target.STOPPED, expected_vm_uuid=vm.vm_uuid)
    platform = _GatePlatform()
    node, _ = _node(db, platform, vm)
    with native_mutation_guard(db.path, vm.name), pytest.raises(NativeSetupBusyError), node.hold_active():
        pytest.fail("live diagnostic skipped guarded admission")
    with node.hold_active():
        pass
    assert platform.holds == 0


@pytest.mark.windows
def test_explicit_guard_token_reuses_outer_operation_for_auto_start_and_hold(db, monkeypatch):
    from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard

    vm = _seed(db)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    with native_mutation_guard(db.path, vm.name) as guard:
        site = VMSiteNode("stub", cast("VMPlatform", platform), (), cast("Registry", object()))
        node = LiveVMNode(db, cast("Config", object()), cast("Registry", object()), vm, site, held_native_guard=guard)
        monkeypatch.setattr(vm_manager, "_await_tailnet", lambda *a, **k: None)
        node.auto_start(cast("SecretReader", object()))
        with node.hold_active(), pytest.raises(NativeSetupBusyError), native_mutation_guard(db.path, vm.name):
            pytest.fail("outer operation guard released")
    assert platform.start_calls == 1
    assert platform.holds == 2
    with pytest.raises(StateError), node.hold_active():
        pytest.fail("completed token accepted")


@pytest.mark.parametrize("replaced", [False, True])
def test_deleted_and_replaced_vm_node_never_uses_stale_row(db, replaced):
    vm = _seed(db)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)
    db.delete_vm(vm.name)
    if replaced:
        _seed(db)
    with pytest.raises(StateError):
        node.auto_start(cast("SecretReader", object()))
    with pytest.raises(StateError), node.hold_active():
        pytest.fail("stale VM entered hold")
    assert platform.start_calls == 0 and platform.holds == 0
