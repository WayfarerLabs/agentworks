"""VM intent acceptance and prepared runtime operations using real resolution."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

from agentworks.bootstrap import load_request_registry
from agentworks.db import Database, ProvisioningStatus, VMRow, VMStatus
from agentworks.db import DesiredRuntimeState as Target
from agentworks.errors import ConnectivityError, StateError, UserAbort, ValidationError
from agentworks.execution import trust
from agentworks.execution.errors import TrustMissing
from agentworks.harness_setup.locking import NativeSetupBusyError, native_mutation_guard
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import TtyInteractionPolicy as Policy
from agentworks.secrets.resolver import Resolver
from agentworks.vms import manager
from agentworks.vms.manager.runtime import activate_vm_runtime, execute_vm_stop, observe_vm_runtime, prepare_vm_runtime
from agentworks.vms.manager.tailscale import _await_tailnet
from tests.conftest import seed_vm_trust


@pytest.fixture(autouse=True)
def _on_tailscale(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workstation is on Tailscale with a supported client, whatever the test host is: start checks both first."""
    from agentworks.execution.client import SSHClient

    monkeypatch.setattr(manager, "verify_tailscale_available", lambda: None)
    monkeypatch.setattr("agentworks.execution.resolve_ssh", lambda: SSHClient("/usr/bin/ssh", (9, 2)))


def _seed(db: Database, target: Target = Target.ON_DEMAND, name: str = "box") -> VMRow:
    vm = db.insert_vm(name, "proxmox", name, desired_runtime_state=target)
    db.update_vm_tailscale(name, "100.64.0.9")
    db.update_vm_provisioning_status(name, ProvisioningStatus.COMPLETE)
    seed_vm_trust(name)
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
    monkeypatch.setattr(manager, "_await_tailnet", lambda *a, **k: events.append("tailscale"))
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
        assert not context.started
    assert resolve_counter == [["proxmox-token"]]
    assert db.get_vm("box") == vm
    assert "hold-enter" not in events


@pytest.mark.parametrize("initial", [VMStatus.STOPPED, VMStatus.RUNNING])
def test_explicit_start_adopts_running_and_waits_inside_the_hold(
    db, make_config, platform, monkeypatch, resolve_counter, captured_output, initial
):
    """The start says what it did before its one wait, which runs inside the hold. Only the site's
    secret resolves, whatever Tailscale key the source chain holds: repairing a VM that does not
    answer is recover's job."""
    config = make_config()
    events, state = platform
    state[0] = initial
    _seed(db, Target.STOPPED)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    manager.start_vm(db, config, "box", interaction=Policy.REFUSE)
    row = db.get_vm("box")
    assert row.desired_runtime_state is Target.RUNNING
    assert events.count("start") == (initial is VMStatus.STOPPED)
    assert events[-3:] == ["hold-enter", "tailscale", "hold-exit"]
    assert resolve_counter == [["proxmox-token"]]
    assert (row.last_started_at is not None) == (initial is VMStatus.STOPPED)
    said = "VM 'box' started" if initial is VMStatus.STOPPED else "VM 'box' is already running"
    assert said in captured_output.info


@pytest.mark.parametrize("answers", [True, False], ids=["answers", "silent"])
def test_start_waits_for_ssh_and_dispatches_nothing_to_the_root_channel(
    db, make_config, platform, monkeypatch, answers: bool
):
    """The real wait runs inside the hold; a VM that does not answer fails naming recover."""
    from agentworks.execution import reachability
    from agentworks.vms.manager.tailscale import VMNotAnswering

    events, _ = platform
    _seed(db)
    monkeypatch.setattr(manager, "_await_tailnet", _await_tailnet)
    monkeypatch.setattr(ProxmoxPlatform, "run_as_root", lambda *a, **k: pytest.fail("a script was dispatched"))

    def _wait(access: object, *, wording: reachability.Wording) -> bool:
        events.append(f"wait:{wording.name}")
        return answers

    monkeypatch.setattr(reachability, "wait", _wait)
    if answers:
        manager.start_vm(db, make_config(), "box", interaction=Policy.REFUSE)
    else:
        with pytest.raises(VMNotAnswering):
            manager.start_vm(db, make_config(), "box", interaction=Policy.REFUSE)
    assert events[-3:] == ["hold-enter", "wait:RECONNECT", "hold-exit"]


@pytest.mark.parametrize("refusal", ["off-tailscale", "unsupported-client"])
def test_start_refuses_before_accepting_intent_resolving_or_touching_the_vm(
    db, make_config, platform, monkeypatch, resolve_counter, refusal
):
    """The wait after the start reaches the VM over the tailnet with the OpenSSH client, so both are checked first."""
    import agentworks.execution as execution
    from agentworks.errors import AgentworksError
    from agentworks.execution.errors import ClientUnsupported

    original = _seed(db, Target.STOPPED)

    def _refuse() -> None:
        raise ConnectivityError("not on Tailscale") if refusal == "off-tailscale" else ClientUnsupported("too old")

    if refusal == "off-tailscale":
        monkeypatch.setattr(manager, "verify_tailscale_available", _refuse)
    else:
        monkeypatch.setattr(execution, "resolve_ssh", _refuse)
    with pytest.raises(AgentworksError):
        manager.start_vm(db, make_config(), "box", interaction=Policy.REFUSE)
    assert resolve_counter == []
    assert platform[0] == []
    assert db.get_vm("box") == original


class _ReachedTrust(Exception):
    """Recover got as far as re-establishing the missing record."""


def test_a_stopped_vm_without_a_host_key_record_starts_then_fails_naming_recover_which_can_then_run(
    db, make_config, platform, monkeypatch, captured_output
):
    """A VM from before the host-key store, stopped while idle: the start boots it, and the real wait's
    first probe raises TrustMissing (whose hint names recover) before anything spawns. Recover, which
    refuses a stopped VM, then runs on the now running VM."""
    config = make_config()
    events, _ = platform
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "ts-key")
    _seed(db, Target.STOPPED)
    trust.remove("box")
    monkeypatch.setattr(manager, "_await_tailnet", _await_tailnet)

    with pytest.raises(TrustMissing):
        manager.start_vm(db, config, "box", interaction=Policy.REFUSE)

    assert "start" in events
    assert "VM 'box' started" in captured_output.info
    assert db.get_vm("box").desired_runtime_state is Target.RUNNING
    monkeypatch.setattr("agentworks.output.is_interactive", lambda: True)

    def _reached(*_args: object, **_kwargs: object) -> None:
        raise _ReachedTrust

    monkeypatch.setattr("agentworks.vms.manager.recover._reestablish_trust", _reached)
    with pytest.raises(_ReachedTrust):
        manager.recover_vm(db, config, "box", interaction=Policy.REFUSE)


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


@pytest.mark.parametrize("action", [manager.start_vm, manager.stop_vm])
def test_local_refusal_does_not_accept_target(db, make_config, resolve_counter, action):
    config = make_config()
    vm = _seed(db)
    db.update_vm_provisioning_status("box", ProvisioningStatus.FAILED)
    with pytest.raises(StateError):
        action(db, config, "box", interaction=Policy.REFUSE)
    assert db.get_vm("box").desired_runtime_state is vm.desired_runtime_state
    assert resolve_counter == []


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
    error = UserAbort("wait cancelled")

    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(manager, "_await_tailnet", fail)
    with _prepared(db, config, vm) as (context, _):
        with pytest.raises(UserAbort) as caught:
            activate_vm_runtime(context)
        assert caught.value is error
        assert context.started
        assert observe_vm_runtime(context) is VMStatus.RUNNING
    assert events.count("hold-enter") == events.count("hold-exit") == 1
    assert db.get_vm("box").desired_runtime_state is Target.ON_DEMAND
    assert db.get_vm("box").last_started_at is not None


def test_stopped_intent_and_unknown_observations_never_activate(db, make_config, platform):
    config = make_config()
    events, state = platform
    vm = _seed(db, Target.STOPPED)
    with _prepared(db, config, vm) as (context, resolver), pytest.raises(StateError):
        activate_vm_runtime(context)
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
            activate_vm_runtime(context)
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
    db.insert_vm("box", "missing-site", "box", desired_runtime_state=Target.ON_DEMAND)
    db.update_vm_provisioning_status("box", ProvisioningStatus.COMPLETE)
    vm = db.get_vm("box")
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
    second = _seed(db, name="peer")
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


@pytest.mark.parametrize("malformed", [None, "running", "provider-payload-sensitive"])
@pytest.mark.parametrize("operation", ["activate", "stop"])
def test_invalid_provider_power_result_refuses_runtime_mutation(db, make_config, platform, malformed, operation):
    config = make_config()
    events, state = platform
    vm = _seed(db)
    with _prepared(db, config, vm) as (context, resolver):
        assert context.observed_status is VMStatus.STOPPED
        state[0] = malformed
        with pytest.raises(StateError) as caught:
            if operation == "activate":
                activate_vm_runtime(context)
            else:
                execute_vm_stop(context)
        assert "provider-payload-sensitive" not in repr((caught.value.args, vars(caught.value)))
        assert not context.started
    assert "start" not in events and "stop" not in events and "hold-enter" not in events
    assert db.get_vm("box") == vm


def test_runtime_activation_rechecks_host_key_before_power(db, make_config, platform):
    vm = _seed(db, Target.RUNNING)
    events, _ = platform
    with _prepared(db, make_config(), vm) as (context, resolver):
        assert events == ["status"]
        trust.remove(vm.name)
        with pytest.raises(TrustMissing):
            activate_vm_runtime(context)
        assert events == ["status"]
        assert not context.started
        assert db.get_vm(vm.name) == vm


def test_vm_scope_reaches_node_readiness(db, make_config, platform, monkeypatch):
    from agentworks.capabilities.base import ScopeLevel

    _seed(db)
    scopes = []
    real = ProxmoxPlatform.preflight

    def _recording(self, ctx):
        scopes.append(ctx.operation_scope)
        real(self, ctx)

    monkeypatch.setattr(ProxmoxPlatform, "preflight", _recording)
    manager.start_vm(db, make_config(), "box", interaction=Policy.REFUSE)
    (scope,) = scopes
    assert scope is not None
    assert scope.level is ScopeLevel.VM
    assert scope.vm == "box"
    assert scope.workspace is None and scope.agent is None and scope.session is None
