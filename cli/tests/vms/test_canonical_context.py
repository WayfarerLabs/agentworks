"""Canonical VM composition with actual claims, hold custody and packed helpers.

Provider and SSH delivery are synthetic. This does not prove native credentials.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.capabilities.vm_platform.wsl2 import WSL2Platform
from agentworks.db import VMStatus
from agentworks.db.operations import OperationRepository
from agentworks.errors import ConfigError, StateError, ValidationError
from agentworks.execution import _wsl2_owned_operation
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from agentworks.execution._target_identity import prepare_target_identity
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHCarrier
from agentworks.execution.carriers.wsl2 import WSL2Carrier
from agentworks.execution.models import Command, Lifetime
from agentworks.execution.profiles import Protection
from agentworks.execution.target import ExecutionRoute
from agentworks.vms import _native_operation
from agentworks.vms._native_operation import VMOperationControlFact, vm_execution_context
from agentworks.vms._wsl2_native_access import WSL2OwnedNativePlatformAccess
from agentworks.vms.target_preparation import (
    VMTargetPreparationControlFact,
    VMTargetPreparationStatus,
    prepare_managed_vm_target_from_platform,
)
from tests.vms.canonical_support import ADDRESS, canonical_config, install_canonical_delivery
from tests.vms.test_native_operation import _install_route, _root, _scope
from tests.vms.test_native_operation import database as database

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="packed Linux body fixture")
_SSH_EXECUTE = SSHCarrier.execute


@pytest.fixture
def canonical(database, tmp_path, monkeypatch):
    platform = WSL2Platform("wsl2", {})
    route, native, observer = _install_route(database, platform, monkeypatch)
    database.update_vm_tailscale("box", ADDRESS)
    config = canonical_config(tmp_path)
    connections = install_canonical_delivery(monkeypatch, route)
    accesses = []
    original = platform.build_native_execution_access

    def deny(*args, **kwargs):
        pytest.fail("canonical execution selected native delivery")

    def build(*args, **kwargs):
        access = original(*args, **kwargs)
        assert isinstance(access, WSL2OwnedNativePlatformAccess)
        accesses.append(access)
        access.native_configuration_check = deny
        monkeypatch.setattr(access, "prepare_native_route", deny)
        return access

    monkeypatch.setattr(platform, "build_native_execution_access", build)
    monkeypatch.setattr(WSL2Carrier, "execute", deny)
    monkeypatch.setattr(_wsl2_owned_operation.WSL2OwnedOperation, "start_and_prepare", deny)
    monkeypatch.setattr(_wsl2_owned_operation.WSL2OwnedOperation, "require_selected_route", deny)
    p = SimpleNamespace(
        platform=platform,
        route=route,
        native=native,
        observer=observer,
        config=config,
        accesses=accesses,
        connections=connections,
        deadline=Deadline.after(30),
        root=_root(tmp_path),
    )

    def operation(**kwargs):
        return vm_execution_context(
            database,
            "box",
            platform,
            RunContext(config=config),
            route=ExecutionRoute.CANONICAL,
            deadline=p.deadline,
            trusted_root=p.root,
            **kwargs,
        )

    p.operation = operation
    return p


@pytest.mark.parametrize("endpoint", [None, "alias", "100.64.0.7\n", "::1", "001.2.3.4", ""])
def test_literal_canonical_endpoint_required_before_provider_effects(canonical, database, endpoint):
    p = canonical
    database.update_vm_tailscale("box", endpoint)
    with pytest.raises(StateError), p.operation():
        pytest.fail("invalid canonical endpoint admitted body")
    assert not p.accesses and not p.native.events and not p.connections
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("setting", ["missing", "identity", "trust", "agent", "client"])
def test_explicit_ssh_policy_validated_passively_before_provider(canonical, database, tmp_path, setting):
    p = canonical
    settings = p.config.operator.ssh
    if setting == "missing":
        p.config.operator.ssh = None
    elif setting == "identity":
        p.config.operator.ssh = replace(settings, identity_file=tmp_path / "$KEY")
    elif setting == "trust":
        p.config.operator.ssh = replace(settings, trust_store=tmp_path / "%d")
    elif setting == "agent":
        p.config.operator.ssh = replace(settings, agent_socket="relative")
    else:
        p.config.operator.ssh = replace(settings, ssh_executable="ssh --option")
    with pytest.raises(ConfigError if setting == "missing" else ValidationError), p.operation():
        pytest.fail("invalid SSH configuration admitted body")
    assert not p.accesses and not p.native.events and not p.connections
    assert database.operations.inspect(_scope()) is None


def test_actual_hold_ready_and_only_canonical_common_probe(canonical, database):
    p = canonical
    with p.operation() as ctx:
        access = p.accesses[0]
        assert access.selected is not None and access.selected.ready is not None
        assert access.selected._ready_is_durable(access.selected.ready)
        assert access.selected.preparation is None
        assert p.native.events.count("dispatch") == 1 and p.route.guest.calls == 1
        target = ctx.admin_execution_target()
        assert target is not None and target.route is ExecutionRoute.CANONICAL
        execution = target.execution()
        assert execution is not None
        binding = execution._operation._native_binding
        assert binding is not None and isinstance(binding.carrier, SSHCarrier)
        assert binding._independent_availability is access.independent_availability is None
        assert binding.runtime_selection == RuntimeSelection(RuntimeTargetOS.LINUX)
        assert binding._new_managed_delivery is not None
        fresh = binding._new_managed_delivery()
        assert fresh is not binding.carrier and isinstance(fresh, SSHCarrier)
        assert fresh._connection is binding.carrier._connection
        before = len(p.connections)
        with pytest.raises(StateError):
            execution.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.INDEPENDENT)
        assert len(p.connections) == before
    assert p.native.events.count("dispatch") == 1 and p.observer.events == ["observe"]
    assert database.operations.inspect(_scope()) is None


def test_operator_stop_refuses_canonical_activation_and_guest_work(canonical, database, monkeypatch):
    p = canonical
    database.set_operator_stopped("box", True)
    monkeypatch.setattr(p.platform, "observe_execution_power", lambda *args, **kwargs: VMStatus.STOPPED)
    with pytest.raises(StateError), p.operation():
        pytest.fail("operator-stopped canonical VM admitted a context")
    assert len(p.accesses) == 1 and p.accesses[0].selected is None
    assert not p.native.events and not p.connections and p.route.guest.calls == 0
    assert database.operations.inspect(_scope()) is None


def test_canonical_operation_job_uses_same_claim_and_fresh_ssh_keeper(canonical, database, monkeypatch):
    from tests.execution.test_managed_execution_access import Delivery

    p = canonical
    ordinary = SSHCarrier.execute
    managed = Delivery()
    carriers = []

    def execute(carrier, invocation, *, io, deadline, custody):
        try:
            managed.selected(io)
        except (AssertionError, AttributeError):
            return ordinary(carrier, invocation, io=io, deadline=deadline, custody=custody)
        carriers.append(carrier)
        return managed.execute(invocation, io=io, deadline=deadline, custody=custody)

    monkeypatch.setattr(SSHCarrier, "execute", execute)
    with p.operation() as ctx:
        execution = ctx.admin_execution_target().execution()
        reference = execution.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.OPERATION)
        (run,) = execution._operation.managed_runs
        binding = execution._operation._native_binding
        assert reference.run_id == run.receipt.identity.run_id and run.acknowledged
        assert run._carrier is binding.carrier and run.keeper._carrier is not binding.carrier
        assert isinstance(run.keeper._carrier, SSHCarrier)
        assert run.keeper._carrier._connection is binding.carrier._connection
        assert database.operations.inspect(_scope()) is not None and p.native.events.count("dispatch") == 1
    assert managed.start.calls == 1 and managed.stop.calls == managed.observe.calls == 1
    assert any(carrier is run.keeper._carrier for carrier in carriers)
    assert database.operations.inspect(_scope()) is None


def test_removed_platform_fact_refuses_independent_start_before_delivery(canonical, database, monkeypatch):
    """Inject a changing platform fact; this does not grant WSL independent jobs."""
    p = canonical
    monkeypatch.setattr(
        WSL2OwnedNativePlatformAccess,
        "independent_availability",
        property(lambda _: _IndependentJobAvailability.NO_IDLE_STOP),
    )
    with p.operation() as ctx:
        execution = ctx.admin_execution_target().execution()
        binding = execution._operation._native_binding
        assert binding._independent_availability is _IndependentJobAvailability.NO_IDLE_STOP
        before = len(p.connections)
        monkeypatch.setattr(WSL2OwnedNativePlatformAccess, "independent_availability", property(lambda _: None))
        with pytest.raises(StateError):
            execution.start(Command(["/bin/true"]), profile=Protection.MANAGED, lifetime=Lifetime.INDEPENDENT)
        assert len(p.connections) == before and not execution._operation.managed_runs
    assert database.operations.inspect(_scope()) is None


def test_trust_refusal_never_falls_back_to_native_delivery(canonical, database, monkeypatch):
    p = canonical
    p.config.operator.ssh.identity_file.write_text("offline fixture key\n")
    monkeypatch.setattr(SSHCarrier, "execute", _SSH_EXECUTE)
    with pytest.raises(StateError), p.operation():
        pytest.fail("missing trust admitted canonical context")
    assert p.native.events.count("dispatch") == 1
    assert not p.connections and p.route.guest.calls == 0 and not p.route.accounts.calls
    assert database.operations.inspect(_scope()) is None


def test_interrupted_close_blocks_retained_views_before_provider_io(canonical, database, monkeypatch):
    p = canonical
    control = KeyboardInterrupt()
    owner = None
    with pytest.raises(KeyboardInterrupt) as caught, p.operation() as ctx:
        target = ctx.admin_execution_target()
        execution, files = target.execution(), target.files()
        owner = execution._operation._owner
        stop = owner.stop_admission

        def interrupt():
            raise control

        monkeypatch.setattr(owner, "stop_admission", interrupt)
    assert caught.value is control and isinstance(control.__cause__, VMOperationControlFact)
    fact = control.__cause__
    assert fact._workflow.body_admission_closed and database.operations.inspect(_scope()) is not None

    def deny(*args, **kwargs):
        pytest.fail("retained view performed provider I/O after close began")

    with monkeypatch.context() as closed:
        closed.setattr(p.accesses[0], "observe_power", deny)
        closed.setattr(p.accesses[0], "observe_locator", deny)
        with pytest.raises(StateError):
            files.stat(p.root / "missing")
        with pytest.raises(StateError):
            execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
    monkeypatch.setattr(owner, "stop_admission", stop)
    before = len(p.connections)
    fact.retry_cleanup(Deadline.after(10))
    assert len(p.connections) == before and database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("field", ["endpoint", "admin", "marker", "locator", "power", "availability"])
def test_selection_drift_refuses_files_and_commands_without_activation(canonical, database, monkeypatch, field):
    p = canonical
    with p.operation() as ctx:
        target = ctx.admin_execution_target()
        execution, files = target.execution(), target.files()
        before = len(p.connections)
        if field == "endpoint":
            database.update_vm_tailscale("box", "100.64.0.8")
        elif field in {"admin", "marker"}:
            original = database.get_vm("box")
            changes = {"admin_username": "other"} if field == "admin" else {"instance_marker": "f" * 32}
            monkeypatch.setattr(database, "get_vm", lambda _: replace(original, **changes))
        elif field == "locator":
            monkeypatch.setattr(
                p.platform, "observe_provider_locator", lambda *args, **kwargs: ProviderLocator("changed")
            )
        elif field == "power":
            monkeypatch.setattr(p.platform, "observe_execution_power", lambda *args, **kwargs: VMStatus.STOPPED)
        else:
            monkeypatch.setattr(
                type(p.accesses[0]),
                "independent_availability",
                property(lambda _: _IndependentJobAvailability.NO_IDLE_STOP),
            )
        with pytest.raises(StateError):
            execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
        with pytest.raises(StateError):
            files.stat(p.root / "missing")
        assert len(p.connections) == before and p.native.events.count("dispatch") == 1
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("phase", ["guest", "admin", "agent"])
def test_endpoint_change_during_preparation_never_admits_views(canonical, database, monkeypatch, phase):
    p = canonical
    original = prepare_managed_vm_target_from_platform if phase == "guest" else prepare_target_identity
    if phase == "agent":
        database.insert_agent("worker", "box", "recorded-worker")
        p.route.accounts.identities["recorded-worker"] = IdentityExpectation(2401, 2402, (2402,))

    def prepare(*args, **kwargs):
        preparation = original(*args, **kwargs)
        if phase != "agent" or kwargs["workload_account"] == "recorded-worker":
            database.update_vm_tailscale("box", "100.64.0.8")
        return preparation

    hook = "prepare_managed_vm_target_from_platform" if phase == "guest" else "prepare_target_identity"
    monkeypatch.setattr(_native_operation, hook, prepare)
    with pytest.raises(StateError), p.operation(agent_name="worker" if phase == "agent" else None):
        pytest.fail("changed canonical selection admitted body")
    assert p.route.guest.calls == 1 and len(p.route.accounts.calls) == {"guest": 0, "admin": 2, "agent": 4}[phase]
    assert database.operations.inspect(_scope()) is None


def test_changed_claim_refuses_before_provider_observation(canonical, database, monkeypatch):
    p = canonical
    with p.operation() as ctx:
        target = ctx.admin_execution_target()
        execution, files = target.execution(), target.files()
        claim = database.operations.inspect(_scope())
        foreign = replace(claim, ownership=replace(claim.ownership, generation_id="f" * 32))

        def deny(*args, **kwargs):
            pytest.fail("stale claim observed a provider")

        with monkeypatch.context() as stale:
            stale.setattr(OperationRepository, "inspect", lambda repository, scope: foreign)
            stale.setattr(p.accesses[0], "observe_power", deny)
            with pytest.raises(StateError):
                execution.run(Command(["/bin/true"]), profile=Protection.DIRECT)
            with pytest.raises(StateError):
                files.stat(p.root / "missing")
    assert database.operations.inspect(_scope()) is None


def test_interrupted_shared_preparation_keeps_exact_core_custody(canonical, database, monkeypatch):
    p = canonical
    original = prepare_managed_vm_target_from_platform
    control = KeyboardInterrupt()
    partial = None

    def prepare(*args, **kwargs):
        nonlocal partial
        prepared = original(*args, **kwargs)
        partial = replace(
            prepared,
            status=VMTargetPreparationStatus.UNCERTAIN,
            pending_remote_effects=True,
            requires_owner_retention=True,
        )
        raise control from VMTargetPreparationControlFact(partial)

    monkeypatch.setattr(_native_operation, "prepare_managed_vm_target_from_platform", prepare)
    with pytest.raises(KeyboardInterrupt) as caught, p.operation():
        pytest.fail("interrupted guest preparation admitted body")
    assert caught.value is control and isinstance(control.__cause__, VMOperationControlFact)
    fact = control.__cause__
    assert fact._workflow.preparation is partial
    assert database.operations.inspect(_scope()) is not None
    with pytest.raises(StateError):
        fact.retry_cleanup(Deadline.after(10))
    assert partial is not None
    fact._workflow.preparation = replace(
        partial,
        status=VMTargetPreparationStatus.PREPARED,
        pending_remote_effects=False,
        requires_owner_retention=False,
    )
    before = len(p.connections)
    fact.retry_cleanup(Deadline.after(10))
    assert len(p.connections) == before and database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("field", ["instance_marker", "boot_id", "init_start_ticks"])
def test_held_guest_mismatch_refuses_before_accounts(canonical, database, monkeypatch, field):
    from tests.execution import test_wsl2_owned_download as guest_fixture

    p = canonical
    if field == "instance_marker":
        monkeypatch.setattr(guest_fixture, "_MARKER", "f" * 32)
    elif field == "boot_id":
        monkeypatch.setattr(guest_fixture, "BOOT", "123e4567-e89b-12d3-a456-426614174000")
    else:
        p.route.guest.init_ticks = 8192
    with pytest.raises(StateError), p.operation():
        pytest.fail("mismatched guest admitted body")
    assert p.route.guest.calls == 1 and not p.route.accounts.calls
    assert database.operations.inspect(_scope()) is None


@pytest.mark.parametrize("route", [ExecutionRoute.PLATFORM_HOST, "canonical", None])
def test_vm_context_requires_explicit_supported_route(database, tmp_path, monkeypatch, route):
    platform = WSL2Platform("wsl2", {})
    monkeypatch.setattr(
        platform, "build_native_execution_access", lambda *args, **kwargs: pytest.fail("provider build")
    )
    with (
        pytest.raises(ValidationError),
        vm_execution_context(
            database,
            "box",
            platform,
            RunContext(),
            route=route,
            deadline=Deadline.after(10),
            trusted_root=_root(tmp_path),
        ),
    ):
        pytest.fail("unsupported route admitted body")
    assert database.operations.inspect(_scope()) is None
