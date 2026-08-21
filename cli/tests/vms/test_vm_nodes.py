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

from agentworks.db import VMStatus
from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.orchestration.activation import activation_gate, ensure_active
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from agentworks.vms.nodes import LiveVMNode, VMSiteNode

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from agentworks.capabilities.base import SecretReader
    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.config import Config
    from agentworks.db import Database, VMRow
    from agentworks.resources.registry import Registry


def _patch_actual_gate_ensure_sequence(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    failure_stage: str | None = None,
    failure: BaseException | None = None,
) -> dict[str, int]:
    import agentworks.ssh_config as ssh_config
    import agentworks.transports as transports
    from agentworks.vms.manager.tailscale import _ensure_tailscale as actual_ensure

    calls = {"verify": 0, "native": 0, "rejoin": 0, "final-wait": 0, "sync": 0}

    def _raise_at(stage: str) -> None:
        if failure_stage == stage:
            assert failure is not None
            raise failure

    class _Target:
        logger = None

    def _verify() -> None:
        calls["verify"] += 1

    def _native(*args: object, **kwargs: object) -> _Target:
        calls["native"] += 1
        return _Target()

    def _rejoin(*args: object, **kwargs: object) -> None:
        calls["rejoin"] += 1
        events.append("rejoin")
        _raise_at("rejoin")
        database = cast("Database", args[0])
        name = cast("str", args[1])
        database.update_vm_tailscale(name, "100.64.0.10")

    def _final_wait(*args: object, **kwargs: object) -> bool:
        calls["final-wait"] += 1
        events.append("final-wait")
        _raise_at("final-wait")
        return True

    def _sync(*args: object, **kwargs: object) -> None:
        calls["sync"] += 1

    monkeypatch.setattr(vm_manager, "verify_tailscale_available", _verify)
    monkeypatch.setattr(vm_manager, "rejoin_tailscale", _rejoin)
    monkeypatch.setattr(transports, "native_transport", _native)
    monkeypatch.setattr(transports, "transport", lambda *args, **kwargs: object())
    monkeypatch.setattr(transports, "wait_for_reconnect", _final_wait)
    monkeypatch.setattr(ssh_config, "sync_ssh_config", _sync)

    def _recording_ensure(*args: object, **kwargs: object) -> None:
        events.append("ensure-enter")
        cast("Callable[..., None]", actual_ensure)(*args, **kwargs)
        events.append("ensure-return")

    monkeypatch.setattr(vm_manager, "_ensure_tailscale", _recording_ensure)
    return calls


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
    monkeypatch.setattr(node, "repair_secret_refs", lambda: ("tailscale-auth-key",))

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
    monkeypatch.setattr(node, "repair_secret_refs", lambda: ("tailscale-auth-key",))
    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", lambda *a, **k: True)
    monkeypatch.setattr(
        vm_manager,
        "_ensure_tailscale",
        lambda *a, **k: platform.events.append("tailscale"),
    )

    ensure_active(node, lambda name: "ts-key")
    assert platform.events == [
        "status",
        "start",
        "hold-open",
        "tailscale",
        "hold-close",
    ]


def test_auto_resume_healthy_probe_releases_without_auth_reader_access(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)

    def _no_repair_names() -> tuple[str, ...]:
        raise AssertionError("healthy auto-start acquired a repair name")

    monkeypatch.setattr(node, "repair_secret_refs", _no_repair_names)
    monkeypatch.setattr(
        vm_manager,
        "_tailscale_rejoin_required",
        lambda *args, **kwargs: platform.events.append("probe-false") or False,
    )

    ensure_active(node, _no_resolve)

    assert platform.events[2:] == ["hold-open", "probe-false", "hold-close"]


def test_auto_resume_rejoin_orders_probe_reader_ensure_inside_hold(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)

    repair_name_calls = 0

    def _repair_names() -> tuple[str, ...]:
        nonlocal repair_name_calls
        repair_name_calls += 1
        if repair_name_calls == 1:
            platform.events.append("repair-name")
        return ("tailscale-auth-key",)

    monkeypatch.setattr(node, "repair_secret_refs", _repair_names)
    monkeypatch.setattr(
        vm_manager,
        "_tailscale_rejoin_required",
        lambda *args, **kwargs: platform.events.append("probe-true") or True,
    )

    def _resolve(name: str) -> str:
        platform.events.append("gate-reader-read")
        return "ts-key"

    calls = _patch_actual_gate_ensure_sequence(monkeypatch, platform.events)

    values = ensure_active(node, _resolve)

    assert platform.events[2:] == [
        "hold-open",
        "probe-true",
        "repair-name",
        "ensure-enter",
        "gate-reader-read",
        "rejoin",
        "final-wait",
        "ensure-return",
        "hold-close",
    ]
    assert values == {"tailscale-auth-key": "ts-key"}
    assert calls == {"verify": 1, "native": 1, "rejoin": 1, "final-wait": 1, "sync": 1}


def test_auto_resume_multiline_repair_key_preserves_host_after_real_probe(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    import agentworks.transports as transports

    auth_key = "tskey-node-sentinel\r\ninjected\r\n"
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)
    monkeypatch.setattr(node, "repair_secret_refs", lambda: ("tailscale-auth-key",))
    monkeypatch.setattr(
        transports,
        "transport",
        lambda *args, **kwargs: platform.events.append("probe-transport") or object(),
    )
    monkeypatch.setattr(
        transports,
        "wait_for_reconnect",
        lambda *args, **kwargs: platform.events.append("repair-required") or False,
    )
    monkeypatch.setattr(
        vm_manager,
        "verify_tailscale_available",
        lambda: pytest.fail("line-unsafe late key reached Tailscale availability work"),
    )

    def _resolve(name: str) -> str:
        assert name == "tailscale-auth-key"
        platform.events.append("late-resolve")
        return auth_key

    with pytest.raises(ValidationError) as caught:
        ensure_active(node, _resolve)

    assert platform.events == [
        "status",
        "start",
        "hold-open",
        "probe-transport",
        "repair-required",
        "late-resolve",
        "hold-close",
    ]
    refreshed = db.get_vm("gvm")
    assert refreshed is not None
    assert refreshed.tailscale_host == "100.64.0.9"
    assert "tskey-node-sentinel" not in repr((caught.value.args, vars(caught.value)))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("failure_stage", ["probe", "acquisition", "auth-read", "rejoin", "final-wait"])
def test_auto_resume_tailscale_failure_matrix_releases_once_without_retry(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
    failure_stage: str,
    failure_type: type[BaseException],
) -> None:
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)
    failure = failure_type(failure_stage)

    def _stage(name: str) -> None:
        platform.events.append(name)
        if failure_stage == name:
            raise failure

    def _probe(*args: object, **kwargs: object) -> bool:
        _stage("probe")
        return True

    repair_name_calls = 0

    def _repair_names() -> tuple[str, ...]:
        nonlocal repair_name_calls
        repair_name_calls += 1
        if repair_name_calls == 1:
            _stage("acquisition")
        return ("tailscale-auth-key",)

    def _resolve(name: str) -> str:
        _stage("auth-read")
        return "ts-key"

    monkeypatch.setattr(node, "repair_secret_refs", _repair_names)
    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", _probe)
    calls = _patch_actual_gate_ensure_sequence(
        monkeypatch,
        platform.events,
        failure_stage=failure_stage,
        failure=failure,
    )

    with pytest.raises(failure_type) as caught:
        ensure_active(node, _resolve)

    assert caught.value is failure
    assert platform.events.count(failure_stage) == 1
    assert platform.events.count("hold-open") == platform.events.count("hold-close") == 1
    assert platform.events[-1] == "hold-close"
    expected_inside_hold = {
        "probe": ["hold-open", "probe", "hold-close"],
        "acquisition": ["hold-open", "probe", "acquisition", "hold-close"],
        "auth-read": [
            "hold-open",
            "probe",
            "acquisition",
            "ensure-enter",
            "auth-read",
            "hold-close",
        ],
        "rejoin": [
            "hold-open",
            "probe",
            "acquisition",
            "ensure-enter",
            "auth-read",
            "rejoin",
            "hold-close",
        ],
        "final-wait": [
            "hold-open",
            "probe",
            "acquisition",
            "ensure-enter",
            "auth-read",
            "rejoin",
            "final-wait",
            "hold-close",
        ],
    }
    assert platform.events[2:] == expected_inside_hold[failure_stage]
    no_ensure_work = {"verify": 0, "native": 0, "rejoin": 0, "final-wait": 0, "sync": 0}
    expected_calls = {
        "probe": no_ensure_work,
        "acquisition": no_ensure_work,
        "auth-read": no_ensure_work,
        "rejoin": {"verify": 1, "native": 1, "rejoin": 1, "final-wait": 0, "sync": 0},
        "final-wait": {"verify": 1, "native": 1, "rejoin": 1, "final-wait": 1, "sync": 0},
    }
    assert calls == expected_calls[failure_stage]


def test_manually_stopped_raises_and_skips_the_ping(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_manually_stopped_but_running_out_of_band_proceeds(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Oracle: the flag is intent, not observed state; RUNNING proceeds
    without a start and without raising."""
    _seed(db)
    db.set_operator_stopped("gvm", True)
    vm = db.get_vm("gvm")
    assert vm is not None
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.RUNNING)
    node, _ = _node(db, platform, vm)
    monkeypatch.setattr(node, "repair_secret_refs", lambda: ("tailscale-auth-key",))

    ensure_active(node, lambda name: "ts-key")
    assert platform.start_calls == 0


def test_flag_is_reread_before_auto_start(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", lambda *a, **k: True)
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: None)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)
    monkeypatch.setattr(node, "repair_secret_refs", lambda: ("tailscale-auth-key",))

    ensure_active(node, lambda name: "ts-key")
    assert platform.start_calls == 1


def test_deallocated_auto_resumes_like_stopped(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", lambda *a, **k: True)
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", lambda *a, **k: None)
    platform = _GatePlatform(status=VMStatus.DEALLOCATED)
    node, _ = _node(db, platform, vm)
    monkeypatch.setattr(node, "repair_secret_refs", lambda: ("tailscale-auth-key",))

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


def test_rejoin_auth_key_reads_lazily_through_the_gate_reader(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    """The repair path hands ensure the unchanged lazy gate reader."""
    vm = _seed(db)
    monkeypatch.setattr(vm_manager, "_is_tailscale_reachable", lambda host: False)
    platform = _GatePlatform(status=VMStatus.STOPPED)
    node, _ = _node(db, platform, vm)
    monkeypatch.setattr(node, "repair_secret_refs", lambda: ("tailscale-auth-key",))
    captured: dict[str, object] = {}

    def _capture(*a: object, **k: object) -> None:
        auth_keys = cast("SecretReader", k["auth_keys"])
        auth_key_name = cast("str", k["auth_key_name"])
        captured["reader"] = auth_keys
        captured["name"] = auth_key_name
        captured["value"] = auth_keys.get(auth_key_name)

    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", lambda *a, **k: True)
    monkeypatch.setattr(vm_manager, "_ensure_tailscale", _capture)
    resolved: list[str] = []

    def _resolve(name: str) -> str:
        resolved.append(name)
        return "ts-key"

    values = ensure_active(node, _resolve)
    assert resolved == ["tailscale-auth-key"]
    assert captured["name"] == "tailscale-auth-key"
    assert captured["value"] == "ts-key"
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
    with pytest.raises(ConfigError, match="cannot pass preflight"):
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
