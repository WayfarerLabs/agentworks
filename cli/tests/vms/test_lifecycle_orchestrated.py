"""VM intent acceptance and prepared runtime operations using real resolution."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

from agentworks.bootstrap import load_request_registry
from agentworks.db import Database, VMRow, VMStatus
from agentworks.db import DesiredRuntimeState as Target
from agentworks.errors import ConnectivityError, StateError, UserAbort, ValidationError
from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy as Policy
from agentworks.secrets.resolver import Resolver
from agentworks.vms import manager
from agentworks.vms.manager.runtime import activate_vm_runtime, execute_vm_stop, observe_vm_runtime, prepare_vm_runtime


def _seed(db: Database, target: Target = Target.ON_DEMAND) -> VMRow:
    vm = db.insert_vm("box", "proxmox", "box", desired_runtime_state=target)
    db.update_vm_tailscale("box", "100.64.0.9")
    row = db.get_vm(vm.name)
    assert row is not None
    return row


@pytest.fixture
def platform(monkeypatch):
    events = []
    state = [VMStatus.STOPPED]

    def status(self, row, ctx):
        assert ctx.secret("proxmox-token") == "pve-token"
        events.append("status")
        return state[0]

    def start(self, row, ctx):
        events.append("start")
        state[0] = VMStatus.RUNNING

    def stop(self, row, ctx):
        events.append("stop")
        state[0] = VMStatus.STOPPED

    @contextlib.contextmanager
    def hold(self, row, *, config):
        events.append("hold-enter")
        try:
            yield
        finally:
            events.append("hold-exit")

    monkeypatch.setattr(ProxmoxPlatform, "status", status)
    monkeypatch.setattr(ProxmoxPlatform, "start", start)
    monkeypatch.setattr(ProxmoxPlatform, "stop", stop)
    monkeypatch.setattr(ProxmoxPlatform, "vm_active", hold)
    monkeypatch.setattr(manager, "_tailscale_rejoin_required", lambda *a, **k: False)
    return events, state


@contextlib.contextmanager
def _prepared(db, config, vm) -> Iterator[tuple]:
    registry = load_request_registry(config, live_database=db)
    resolver = Resolver(config, registry, interaction=Policy.REFUSE)
    with native_mutation_guard(db.path, vm.name) as guard:
        context = prepare_vm_runtime(
            db,
            config,
            registry,
            vm,
            resolve_secret=resolver.resolve_gate,
            held_native_guard=guard,
        )
        try:
            yield context, resolver
        finally:
            context.clear_values()
            resolver.close()


def test_preparation_only_observes_and_execution_uses_supplied_values(db, make_config, platform, resolve_counter):
    config = make_config()
    events, _ = platform
    vm = _seed(db)
    with _prepared(db, config, vm) as (context, resolver):
        assert context.observed_status is VMStatus.STOPPED
        assert events == ["status"]
        assert not execute_vm_stop(context)
        assert observe_vm_runtime(context) is VMStatus.STOPPED
        assert not context.started and not context.repaired
    assert resolve_counter == [["proxmox-token"]]
    assert db.get_vm("box") == vm
    assert "hold-enter" not in events


@pytest.mark.parametrize("initial", [VMStatus.STOPPED, VMStatus.RUNNING])
def test_explicit_start_adopts_running_and_repairs_conditionally(
    db, make_config, platform, monkeypatch, resolve_counter, initial
):
    config = make_config()
    events, state = platform
    state[0] = initial
    _seed(db, Target.STOPPED)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    monkeypatch.setattr(manager, "_tailscale_rejoin_required", lambda *a, **k: True)

    def repair(*args, auth_keys, auth_key_name, **kwargs):
        assert events[-1] == "hold-enter"
        assert auth_keys.get(auth_key_name) == "ts-key"
        events.append("repair")

    monkeypatch.setattr(manager, "_ensure_tailscale", repair)
    manager.start_vm(db, config, "box", interaction=Policy.REFUSE)
    row = db.get_vm("box")
    assert row.desired_runtime_state is Target.RUNNING
    assert events.count("start") == (initial is VMStatus.STOPPED)
    assert events[-3:] == ["hold-enter", "repair", "hold-exit"]
    assert resolve_counter == [["proxmox-token"], ["tailscale-auth-key"]]
    assert (row.last_started_at is not None) == (initial is VMStatus.STOPPED)


def test_healthy_start_does_not_collect_speculative_repair_secret(db, make_config, platform, resolve_counter):
    config = make_config()
    _seed(db)
    manager.start_vm(db, config, "box", interaction=Policy.REFUSE)
    assert resolve_counter == [["proxmox-token"]]


@pytest.mark.parametrize("action,target", [(manager.start_vm, Target.RUNNING), (manager.stop_vm, Target.STOPPED)])
@pytest.mark.parametrize(
    "error", [ConnectivityError("failed credentials"), UserAbort("cancelled"), KeyboardInterrupt()]
)
def test_accepted_target_survives_credential_failure_or_cancellation(
    db, make_config, monkeypatch, action, target, error
):
    config = make_config()
    vm = _seed(db)

    def fail(self, name):
        raise error

    monkeypatch.setattr(Resolver, "resolve_gate", fail)
    with pytest.raises(type(error)) as caught:
        action(db, config, "box", interaction=Policy.REFUSE)
    assert caught.value is error
    row = db.get_vm("box")
    assert row.desired_runtime_state is target
    assert row.vm_uuid == vm.vm_uuid
    assert row.last_started_at is None


def test_local_refusal_does_not_accept_target(db, make_config):
    config = make_config()
    vm = _seed(db)
    from agentworks.db import ProvisioningStatus

    db.update_vm_provisioning_status("box", ProvisioningStatus.FAILED)
    with pytest.raises(StateError):
        manager.start_vm(db, config, "box", interaction=Policy.REFUSE)
    assert db.get_vm("box").desired_runtime_state is vm.desired_runtime_state


@pytest.mark.parametrize("initial", [VMStatus.RUNNING, VMStatus.STOPPED, VMStatus.DEALLOCATED])
def test_explicit_stop_accepts_target_without_booting_hold(db, make_config, platform, initial):
    config = make_config()
    events, state = platform
    state[0] = initial
    _seed(db, Target.RUNNING)
    manager.stop_vm(db, config, "box", interaction=Policy.REFUSE)
    assert db.get_vm("box").desired_runtime_state is Target.STOPPED
    assert "hold-enter" not in events
    assert events.count("stop") == (initial is VMStatus.RUNNING)


def test_dependency_activation_preserves_on_demand_and_reports_partial_effects(db, make_config, platform, monkeypatch):
    config = make_config()
    events, _ = platform
    vm = _seed(db)
    error = UserAbort("repair cancelled")
    monkeypatch.setattr(manager, "_tailscale_rejoin_required", lambda *a, **k: True)

    def fail(name):
        raise error

    with _prepared(db, config, vm) as (context, _):
        with pytest.raises(UserAbort) as caught:
            activate_vm_runtime(context, resolve_secret=fail)
        assert caught.value is error
        assert context.started and not context.repaired
        assert observe_vm_runtime(context) is VMStatus.RUNNING
    assert events.count("hold-enter") == events.count("hold-exit") == 1
    assert db.get_vm("box").desired_runtime_state is Target.ON_DEMAND
    assert db.get_vm("box").last_started_at is not None


def test_stopped_intent_and_unknown_observations_never_activate(db, make_config, platform):
    config = make_config()
    events, state = platform
    vm = _seed(db, Target.STOPPED)
    with _prepared(db, config, vm) as (context, resolver), pytest.raises(StateError):
        activate_vm_runtime(context, resolve_secret=resolver.resolve_gate)
    state[0] = VMStatus.UNKNOWN
    vm = db.get_vm("box")
    with _prepared(db, config, vm) as (context, resolver), pytest.raises(StateError):
        execute_vm_stop(context)
    assert "start" not in events and "stop" not in events and "hold-enter" not in events


@pytest.mark.parametrize("change", ["delete", "replace", "target"])
def test_prepared_provider_operations_reject_invalidated_selection(db, make_config, platform, change):
    config = make_config()
    vm = _seed(db)
    with _prepared(db, config, vm) as (context, resolver):
        if change in {"delete", "replace"}:
            db.delete_vm("box")
            if change == "replace":
                _seed(db)
        else:
            db.set_vm_desired_runtime_state("box", Target.STOPPED, expected_vm_uuid=vm.vm_uuid)
        with pytest.raises(StateError):
            activate_vm_runtime(context, resolve_secret=resolver.resolve_gate)
        with pytest.raises(StateError):
            observe_vm_runtime(context)


@pytest.mark.windows
def test_native_guard_contention_prevents_public_acceptance_and_provider_actions(db, make_config, platform):
    config = make_config()
    vm = _seed(db)
    with native_mutation_guard(db.path, "box"):
        with pytest.raises(NativeSetupBusyError):
            manager.start_vm(db, config, "box", interaction=Policy.REFUSE)
        with pytest.raises(NativeSetupBusyError):
            manager.stop_vm(db, config, "box", interaction=Policy.REFUSE)
    assert db.get_vm("box") == vm
    assert platform[0] == []


def test_pure_setter_never_loads_registry_or_runtime(db, monkeypatch):
    vm = _seed(db)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *a, **k: pytest.fail("registry loaded"))
    manager.set_vm_desired_runtime_state(db, name="box", state="stopped")
    assert db.get_vm("box").desired_runtime_state is Target.STOPPED
    assert db.get_vm("box").vm_uuid == vm.vm_uuid
    with pytest.raises(ValidationError):
        manager.set_vm_desired_runtime_state(db, name="box", state="invalid")


@pytest.mark.parametrize("action", [manager.start_vm, manager.stop_vm])
def test_provider_failure_preserves_accepted_target(db, make_config, platform, monkeypatch, action):
    config = make_config()
    _, state = platform
    state[0] = VMStatus.STOPPED if action is manager.start_vm else VMStatus.RUNNING
    _seed(db)
    error = ConnectivityError("provider unavailable")

    def fail(*args):
        raise error

    monkeypatch.setattr(ProxmoxPlatform, "start" if action is manager.start_vm else "stop", fail)
    with pytest.raises(ConnectivityError) as caught:
        action(db, config, "box", interaction=Policy.REFUSE)
    assert caught.value is error
    assert db.get_vm("box").desired_runtime_state is (Target.RUNNING if action is manager.start_vm else Target.STOPPED)


@pytest.mark.parametrize("action", [manager.start_vm, manager.stop_vm])
def test_explicit_unknown_power_policy_is_preserved(db, make_config, platform, action):
    config = make_config()
    events, state = platform
    state[0] = VMStatus.UNKNOWN
    _seed(db)
    db.record_vm_started("box")
    action(db, config, "box", interaction=Policy.REFUSE)
    assert ("start" if action is manager.start_vm else "stop") in events
    if action is manager.start_vm:
        assert db.get_vm("box").last_started_at is None


@pytest.mark.parametrize("action", [manager.start_vm, manager.stop_vm])
def test_known_local_site_refusal_leaves_target_unaccepted(db, make_config, resolve_counter, action):
    from agentworks.errors import AgentworksError

    config = make_config()
    vm = db.insert_vm("box", "missing-site", "box", desired_runtime_state=Target.ON_DEMAND)
    with pytest.raises(AgentworksError):
        action(db, config, "box", interaction=Policy.REFUSE)
    assert db.get_vm("box") == vm
    assert resolve_counter == []


def test_shared_failed_infrastructure_uses_one_resolver_attempt_without_preflight_prediction(
    db, make_config, platform, monkeypatch, resolve_counter
):
    from agentworks.errors import SecretUnavailableError

    config = make_config()
    first = _seed(db)
    second = db.insert_vm("peer", "proxmox", "peer", desired_runtime_state=Target.ON_DEMAND)
    registry = load_request_registry(config, live_database=db)
    monkeypatch.delenv("AW_SECRET_PROXMOX_TOKEN")
    monkeypatch.setattr(
        "agentworks.orchestration.secrets.require_predicted_refs",
        lambda *a, **k: pytest.fail("prepared VM readiness predicted a source outside the Resolver"),
    )
    resolver = Resolver(config, registry, interaction=Policy.REFUSE)
    try:
        for vm in (first, second):
            with native_mutation_guard(db.path, vm.name) as guard, pytest.raises(SecretUnavailableError):
                prepare_vm_runtime(
                    db,
                    config,
                    registry,
                    vm,
                    resolve_secret=resolver.resolve_gate,
                    held_native_guard=guard,
                )
        assert resolve_counter == [["proxmox-token"]]
        assert platform[0] == []
    finally:
        resolver.close()


def test_source_free_vm_preflight_still_rejects_dangling_declared_secret(db, make_config, monkeypatch):
    from agentworks.errors import ConfigError

    config = make_config()
    vm = _seed(db)
    registry = load_request_registry(config, live_database=db)
    lookup = registry.lookup

    def without_secret(kind, name):
        if kind == "secret" and name == "proxmox-token":
            raise KeyError(name)
        return lookup(kind, name)

    monkeypatch.setattr(registry, "lookup", without_secret)
    with native_mutation_guard(db.path, vm.name) as guard, pytest.raises(ConfigError):
        prepare_vm_runtime(
            db,
            config,
            registry,
            vm,
            resolve_secret=lambda name: pytest.fail("dangling declaration reached a source"),
            held_native_guard=guard,
        )
