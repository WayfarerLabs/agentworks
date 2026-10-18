"""Azure composition ordering with concrete reader, exposure and SQLite custody."""

from __future__ import annotations

from contextlib import closing
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope, VMStatus
from agentworks.errors import ConfigError, LimitExceededError, StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.operations import OperationOwner
from agentworks.plugins.azure import _native_execution_access as native
from agentworks.plugins.azure._incarnation_start_payload import StartState
from agentworks.plugins.azure._native_execution_access import AzureOwnedAccess
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.vms._native_operation import _Workflow
from agentworks.vms.target_preparation import (
    VMTargetPreparation,
    VMTargetPreparationControlFact,
    VMTargetPreparationFailure,
    VMTargetPreparationStatus,
)
from tests.plugins.azure.test_native_access import NIC, RESOURCE, UUID
from tests.plugins.azure.test_native_access import owned as owned


class Control(BaseException):
    pass


def prepared() -> VMTargetPreparation:
    target = ManagedTargetIdentity(ManagedTargetKind.VM, "vm", "v1:" + "0" * 64, UUID)
    return VMTargetPreparation(VMTargetPreparationStatus.PREPARED, target, None)


@pytest.fixture
def full(owned, tmp_path, monkeypatch):
    # These are real dependency modules. Route implementation belongs to its
    # sibling effort; no substitute module is installed if it is unavailable.
    from agentworks.plugins.azure import _native_route

    settings = SSHSettings(tmp_path / "trust", tmp_path / "identity")
    owned.vm.site = "site"
    owned.vm.admin_username = "root"
    owned.ctx.config = SimpleNamespace(operator=SimpleNamespace(ssh=settings, ssh_allow_cidrs=[]))
    p = SimpleNamespace(
        read=owned,
        calls=owned.calls,
        power=VMStatus.RUNNING,
        monitor=[StartState.SUCCEEDED],
        start_state=StartState.SUCCEEDED,
        start_error=None,
        start_cleanup=False,
        start_settle_error=None,
        route_cleanup=False,
        route_settled=True,
        route_error=None,
        route_open_hook=lambda: None,
        route_settle_error=None,
        prefix_close_error=None,
        after_start_power="running",
        startup_power_codes=[],
        prep=prepared(),
        prep_error=None,
        pauses=[],
        deadlines=[],
    )

    def event(name: str, deadline: Deadline) -> None:
        assert deadline is p.deadline or deadline is p.cleanup_deadline
        p.deadlines.append(deadline)
        p.calls.append(name)

    class Start:
        def __init__(self, owner, name, credential, resource, vm_id, locator):
            assert owner is p.owner and name == "vm" and credential is owned.credential
            assert resource == RESOURCE and vm_id == UUID and locator.token == f"azure-vm:v2:{UUID}:{RESOURCE}"
            self.state = StartState.UNKNOWN
            self.cleanup_incomplete = False
            self.closed = False
            self.obligation = None
            p.calls.append("start-retained")

        def start(self, deadline):
            assert p.access._start is self
            event("start-post", deadline)
            self.obligation = p.owner.register_lifecycle_obligation(
                "synthetic-azure-start", payload_version=1, payload=b"s"
            )
            self.obligation.mark_possible_effect()
            self.cleanup_incomplete = p.start_cleanup
            if p.start_error is not None:
                raise p.start_error
            self.state = p.start_state
            owned.current.instance_view.statuses[0].code = "PowerState/" + p.after_start_power

        def observe(self, deadline):
            assert not self.closed and not self.cleanup_incomplete
            event("start-monitor", deadline)
            self.state = p.monitor.pop(0) if p.monitor else StartState.PENDING
            return self.state

        def close(self, deadline):
            event("start-close", deadline)
            self.closed = True
            self.cleanup_incomplete = p.start_cleanup
            return not self.cleanup_incomplete

        def settle(self, deadline):
            event("start-settle", deadline)
            self.cleanup_incomplete = p.start_cleanup
            if p.start_settle_error is not None:
                raise p.start_settle_error
            if self.cleanup_incomplete:
                return False
            if self.obligation is None:
                return self.closed
            if self.state in (StartState.SUCCEEDED, StartState.FAILED, StartState.CANCELED):
                self.obligation.resolve()
                return True
            return False

    class Route:
        def __init__(self, owner, name, credential, resource, vm_id, locator, current, prefixes):
            assert owner is p.owner and name == "vm" and credential is owned.credential
            assert resource == RESOURCE and vm_id == UUID and locator.token == f"azure-vm:v2:{UUID}:{RESOURCE}"
            assert current is owned.current and prefixes == ("192.0.2.1/32",)
            self.cleanup_incomplete = False
            self.closed = False
            self.obligation = None
            self.nic_id = current.network_profile.network_interfaces[0].id
            p.calls.append("route-retained")

        def open(self, deadline):
            assert p.access._route is self
            event("route-open", deadline)
            self.obligation = p.owner.register_lifecycle_obligation(
                "synthetic-azure-route", payload_version=1, payload=b"r"
            )
            self.obligation.mark_possible_effect()
            self.cleanup_incomplete = p.route_cleanup
            if p.route_error is not None:
                raise p.route_error
            p.route_open_hook()

        def check(self, deadline, *, observed_vm):
            assert not self.closed
            assert observed_vm is owned.current
            if observed_vm.network_profile.network_interfaces[0].id != self.nic_id:
                raise StateError("synthetic changed selected NIC")
            event("route-check", deadline)

        def close(self, deadline):
            event("route-close", deadline)
            self.closed = True
            self.cleanup_incomplete = p.route_cleanup
            return not self.cleanup_incomplete

        def settle(self, deadline):
            event("route-settle", deadline)
            if p.route_settle_error is not None:
                raise p.route_settle_error
            if p.route_settled and not self.cleanup_incomplete:
                if self.obligation is not None:
                    self.obligation.resolve()
                return True
            return False

    class Response:
        def read(self, limit):
            p.calls.append("prefix-read")
            assert limit == 65 and p.access._exposure.cleanup_incomplete
            return b"192.0.2.1\n"

        def close(self):
            p.calls.append("prefix-close")
            if p.prefix_close_error is not None:
                raise p.prefix_close_error

    def prepare(vm, platform, locator, binding, *, deadline, owner, observe_locator):
        event("guest-prepare", deadline)
        assert vm is owned.vm and platform is owned.platform and owner is p.owner
        assert binding is p.access.binding and p.access.route_check is None
        assert observe_locator(deadline) == locator
        if p.prep_error is not None:
            raise p.prep_error
        return p.prep

    def pause(seconds):
        assert 0 < seconds <= 0.25
        p.pauses.append(seconds)
        owned.now[0] += seconds
        if p.startup_power_codes:
            owned.current.instance_view.statuses[0].code = "PowerState/" + p.startup_power_codes.pop(0)

    monkeypatch.setattr(native, "AzureIncarnationStart", Start)
    monkeypatch.setattr(_native_route, "AzureNativeRoute", Route)
    monkeypatch.setattr(native, "prepare_managed_vm_target_from_platform", prepare)
    monkeypatch.setattr("agentworks.vms._ssh_native_exposure.urlopen", lambda *a, **k: Response())
    monkeypatch.setattr(native, "time", SimpleNamespace(sleep=pause))
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent.toml")
    with closing(Database(tmp_path / "composition.db")) as database:
        p.database = database
        p.owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, "vm"), "azure-composition"
        )
        p.deadline = Deadline.after(10)
        p.cleanup_deadline = None
        p.access = AzureOwnedAccess(owned.vm, owned.platform, owned.ctx, owner=p.owner)
        owned.access = p.access._reader
        yield p


def select(p: Any, power: VMStatus) -> None:
    p.read.current.instance_view.statuses[0].code = "PowerState/" + power.value


def test_factory_is_passive_and_validates_exact_owner(full):
    p = full
    access = AzureVMPlatform.build_native_execution_access(
        p.read.platform, p.read.vm, p.read.ctx, owner=p.owner, custody=LocalDeliveryCustody()
    )
    assert isinstance(access, AzureOwnedAccess) and p.calls == []
    assert access.binding is None and access.preparation is None and access.route_check is None
    p.read.vm.name = "foreign"
    with pytest.raises(ValidationError):
        AzureVMPlatform.build_native_execution_access(
            p.read.platform, p.read.vm, p.read.ctx, owner=p.owner, custody=LocalDeliveryCustody()
        )
    assert p.calls == []


@pytest.mark.parametrize("power", [VMStatus.RUNNING, VMStatus.STOPPED, VMStatus.DEALLOCATED])
def test_success_orders_fresh_reads_route_guest_and_credential_last(full, power):
    p = full
    select(p, power)
    assert p.access.observe_power(p.deadline) is power
    p.access.prepare(power, p.deadline)
    assert p.access.preparation is p.prep and p.access.route_check is not None
    assert p.calls.index("prefix-close") < p.calls.index("route-open") < p.calls.index("guest-prepare")
    assert p.calls.count("start-post") == int(power is not VMStatus.RUNNING)
    assert p.calls.count("vm-get") >= 5
    check = p.access.route_check
    check(p.deadline)
    assert p.calls[-1] == "route-check"
    assert p.access.settle(p.deadline)
    assert p.calls[-1] == "credential-close" and not p.owner.list_pending_lifecycle_obligations()
    before = list(p.calls)
    assert p.access.settle(p.deadline) and p.calls == before
    with pytest.raises(StateError):
        check(p.deadline)
    with pytest.raises(StateError):
        p.access.prepare(power, p.deadline)


@pytest.mark.parametrize("method", ["observe_power", "prepare"])
def test_ssh_policy_required_before_auth_or_reads(full, method):
    p = full
    p.read.ctx.config.operator.ssh = None
    with pytest.raises(ConfigError):
        if method == "prepare":
            p.access.prepare(VMStatus.RUNNING, p.deadline)
        else:
            p.access.observe_power(p.deadline)
    assert p.calls == []
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize("power", [VMStatus.UNKNOWN, "running", None])
def test_unsupported_selection_refuses_before_provider_work(full, power):
    with pytest.raises(StateError):
        full.access.prepare(power, full.deadline)
    assert full.calls == []


@pytest.mark.parametrize("observed", ["stopped", "deallocated", "starting", "unknown"])
def test_changed_initial_power_never_starts_or_opens_route(full, observed):
    p = full
    p.read.current.instance_view.statuses[0].code = "PowerState/" + observed
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert "start-post" not in p.calls and "route-open" not in p.calls
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize("outcome", [StartState.SUCCEEDED, StartState.FAILED, StartState.CANCELED])
def test_async_completion_is_request_evidence_separate_from_availability(full, outcome):
    p = full
    select(p, VMStatus.STOPPED)
    p.start_state = StartState.PENDING
    p.monitor = [StartState.PENDING, outcome]
    if outcome is StartState.SUCCEEDED:
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    else:
        with pytest.raises(StateError):
            p.access.prepare(VMStatus.STOPPED, p.deadline)
        assert p.access.binding is None and "route-open" not in p.calls
    assert p.calls.count("start-post") == 1 and p.calls.count("start-monitor") == 2
    assert p.pauses == [0.25]
    assert p.access.settle(p.deadline)


def test_fresh_cleanup_finishes_existing_monitor_without_preparation_or_repost(full):
    p = full
    select(p, VMStatus.DEALLOCATED)
    p.start_state = StartState.PENDING
    p.monitor = []
    with pytest.raises(LimitExceededError):
        p.access.prepare(VMStatus.DEALLOCATED, p.deadline)
    assert p.access.binding is None and "route-open" not in p.calls
    p.cleanup_deadline = Deadline.after(5)
    p.monitor = [StartState.PENDING, StartState.SUCCEEDED]
    assert p.access.settle(p.cleanup_deadline)
    assert p.calls.count("start-post") == 1 and "guest-prepare" not in p.calls
    assert p.calls.index("start-close") > max(i for i, name in enumerate(p.calls) if name == "start-monitor")
    assert p.calls[-1] == "credential-close"


def test_lost_ack_preserves_owner_and_credential_without_inventing_monitor(full):
    p = full
    select(p, VMStatus.STOPPED)
    p.start_error = StateError("synthetic lost acknowledgment")
    with pytest.raises(StateError) as caught:
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert caught.value is p.start_error
    assert not p.access.settle(p.deadline)
    assert p.owner.list_pending_lifecycle_obligations()
    assert "credential-close" not in p.calls and "start-monitor" not in p.calls
    assert p.calls.count("start-post") == 1
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)


@pytest.mark.parametrize("stage", ["read", "start", "route", "prefix"])
def test_retained_originals_keep_credential_until_positive_cleanup(full, stage):
    p = full
    if stage == "read":
        p.read.compute_close_error = RuntimeError("synthetic compute close")
    elif stage == "start":
        select(p, VMStatus.STOPPED)
        p.start_cleanup = True
    elif stage == "route":
        p.route_cleanup = True
    else:
        p.prefix_close_error = RuntimeError("synthetic source response close")
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.STOPPED if stage == "start" else VMStatus.RUNNING, p.deadline)
    assert not p.access.settle(p.deadline)
    assert "credential-close" not in p.calls
    p.read.compute_close_error = None
    p.start_cleanup = p.route_cleanup = False
    p.prefix_close_error = None
    assert p.access.settle(p.deadline)
    assert p.calls[-1] == "credential-close"


@pytest.mark.parametrize("change", ["uuid", "power", "endpoint", "nic"])
def test_route_check_revalidates_fresh_identity_power_and_endpoint(full, change):
    p = full
    p.access.prepare(VMStatus.RUNNING, p.deadline)
    checks = p.calls.count("route-check")
    if change == "uuid":
        p.read.current.vm_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    elif change == "power":
        select(p, VMStatus.STOPPED)
    elif change == "nic":
        p.read.current.network_profile.network_interfaces[0].id = NIC + "-foreign"
        p.read.nic_id = NIC + "-foreign"
    else:
        p.read.network.public_ip_addresses.get = lambda *a, **k: SimpleNamespace(
            id=p.read.pip_id, properties=SimpleNamespace(ip_address="198.51.100.99")
        )
    with pytest.raises(StateError):
        p.access.route_check(p.deadline)
    assert p.calls.count("route-check") == checks
    assert p.access.settle(p.deadline)


def test_successful_request_waits_for_documented_startup_power_states(full, monkeypatch):
    p = full
    select(p, VMStatus.DEALLOCATED)
    states = ["starting", "stopped", "running"]
    original = p.access._reader.observe_vm

    def observe(deadline):
        assert deadline is p.deadline
        if p.access._start is not None and states:
            p.read.current.instance_view.statuses[0].code = "PowerState/" + states.pop(0)
        return original(deadline)

    monkeypatch.setattr(p.access._reader, "observe_vm", observe)
    p.access.prepare(VMStatus.DEALLOCATED, p.deadline)
    assert states == [] and p.pauses == [0.25, 0.25]
    assert p.calls.count("start-post") == 1
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize(
    "codes",
    [
        None,
        [],
        [None],
        ["PowerState/unknown"],
        ["PowerState/deallocated"],
        ["PowerState/stopping"],
        ["PowerState/deallocating"],
        ["PowerState/starting", "PowerState/running"],
    ],
)
def test_successful_start_refuses_unknown_or_malformed_power_without_waiting(full, monkeypatch, codes):
    p = full
    select(p, VMStatus.STOPPED)
    original = p.access._reader.observe_vm

    def observe(deadline):
        assert deadline is p.deadline
        if p.access._start is not None:
            p.read.current.instance_view.statuses = (
                None if codes is None else [SimpleNamespace(code=code) for code in codes]
            )
        return original(deadline)

    monkeypatch.setattr(p.access._reader, "observe_vm", observe)
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert not p.pauses and p.calls.count("start-post") == 1 and "route-open" not in p.calls
    assert p.access.settle(p.deadline)


def test_startup_power_wait_rechecks_uuid_and_original_deadline(full, monkeypatch):
    p = full
    select(p, VMStatus.STOPPED)
    p.after_start_power = "starting"
    original = p.access._reader.observe_vm

    def observe(deadline):
        assert deadline is p.deadline
        if p.pauses:
            p.read.current.vm_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        return original(deadline)

    monkeypatch.setattr(p.access._reader, "observe_vm", observe)
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert p.pauses == [0.25] and p.calls.count("start-post") == 1
    assert p.access.settle(p.deadline)


def test_startup_power_wait_expires_without_reposting_or_waiting_during_cleanup(full):
    p = full
    select(p, VMStatus.STOPPED)
    p.after_start_power = "starting"
    with pytest.raises(LimitExceededError):
        p.access.prepare(VMStatus.STOPPED, p.deadline)
    assert p.deadline.expired and p.calls.count("start-post") == 1
    assert p.access.binding is None and "route-open" not in p.calls
    reads = p.calls.count("vm-get")
    p.cleanup_deadline = Deadline.after(5)
    assert p.access.settle(p.cleanup_deadline)
    assert p.calls.count("vm-get") == reads and p.calls.count("start-post") == 1


@pytest.mark.parametrize("endpoint", [None, "hostname.invalid", "2001:db8::1", "198.51.100.2:22"])
def test_invalid_endpoint_never_discovers_source_or_opens_route(full, endpoint):
    p = full
    p.read.network.public_ip_addresses.get = lambda *a, **k: SimpleNamespace(
        id=p.read.pip_id, properties=SimpleNamespace(ip_address=endpoint)
    )
    with pytest.raises(StateError):
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert p.access.binding is None and p.access._exposure is None and p.access._route is None
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize("status", [VMTargetPreparationStatus.FAILED, VMTargetPreparationStatus.UNCERTAIN])
@pytest.mark.parametrize("control", [False, True])
def test_partial_preparation_facts_are_retained_without_guest_admission(full, status, control):
    p = full
    retained = status is VMTargetPreparationStatus.UNCERTAIN
    p.prep = VMTargetPreparation(
        status,
        None,
        None,
        VMTargetPreparationFailure.GUEST_OBSERVATION,
        pending_remote_effects=retained,
        requires_owner_retention=retained,
    )
    if control:
        p.prep_error = Control("synthetic guest control")
        p.prep_error.__cause__ = VMTargetPreparationControlFact(p.prep)
        with pytest.raises(Control) as caught:
            p.access.prepare(VMStatus.RUNNING, p.deadline)
        assert caught.value is p.prep_error
    else:
        p.access.prepare(VMStatus.RUNNING, p.deadline)
    assert p.access.preparation is p.prep and p.access.route_check is None
    assert p.access.settle(p.deadline) is not retained


@pytest.mark.parametrize("first", [RuntimeError("synthetic route error"), Control("synthetic route control")])
def test_cleanup_attempts_both_effects_and_preserves_first_escaping_control(full, first):
    p = full
    select(p, VMStatus.STOPPED)
    p.access.prepare(VMStatus.STOPPED, p.deadline)
    p.route_settle_error = first
    p.start_settle_error = Control("synthetic start cleanup control")
    with pytest.raises(Control) as caught:
        p.access.settle(p.deadline)
    assert caught.value is (first if isinstance(first, Control) else p.start_settle_error)
    assert "route-settle" in p.calls and "start-settle" in p.calls
    assert "credential-close" not in p.calls
    p.route_settle_error = p.start_settle_error = None
    assert p.access.settle(p.deadline)


def test_core_workflow_keeps_exact_owner_until_all_components_settle(full):
    p = full
    p.access.prepare(VMStatus.RUNNING, p.deadline)
    p.route_settled = False
    workflow = _Workflow(p.owner, p.deadline, access=p.access)
    with pytest.raises(StateError):
        workflow.close()
    assert p.database.operations.inspect(p.owner.ownership.scope) is not None
    assert "credential-close" not in p.calls
    p.route_settled = True
    p.cleanup_deadline = Deadline.after(5)
    workflow.close(cleanup_deadline=p.cleanup_deadline)
    assert p.database.operations.inspect(p.owner.ownership.scope) is None
    before = list(p.calls)
    assert p.access.settle(p.cleanup_deadline) and p.calls == before


@pytest.mark.parametrize("error", [RuntimeError("synthetic final credential close"), Control("synthetic final close")])
def test_final_credential_retirement_retry_does_not_revisit_settled_effects(full, monkeypatch, error):
    p = full
    select(p, VMStatus.STOPPED)
    p.access.prepare(VMStatus.STOPPED, p.deadline)
    original = p.read.credential.close

    def fail_close():
        raise error

    monkeypatch.setattr(p.read.credential, "close", fail_close)
    if isinstance(error, Control):
        with pytest.raises(Control) as caught:
            p.access.settle(p.deadline)
        assert caught.value is error
    else:
        assert not p.access.settle(p.deadline)
    assert not p.owner.list_pending_lifecycle_obligations()
    before = list(p.calls)
    monkeypatch.setattr(p.read.credential, "close", original)
    monkeypatch.setattr(p.access._route, "settle", lambda _: pytest.fail("settled route revisited"))
    monkeypatch.setattr(p.access._start, "settle", lambda _: pytest.fail("settled start revisited"))
    assert p.access.settle(p.deadline)
    assert p.calls[len(before) :] == ["credential-close"]


def test_cleanup_cannot_close_borrowed_credential_during_route_admission(full):
    p = full
    admitted, release, settling = Event(), Event(), Event()
    finished = Event()
    failures = []

    def hold():
        admitted.set()
        assert release.wait(5)

    def prepare():
        try:
            p.access.prepare(VMStatus.RUNNING, p.deadline)
        except BaseException as error:
            failures.append(error)

    def settle():
        try:
            settling.set()
            assert p.access.settle(p.deadline)
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    p.route_open_hook = hold
    preparing = Thread(target=prepare)
    cleaning = Thread(target=settle)
    preparing.start()
    try:
        assert admitted.wait(5)
        cleaning.start()
        assert settling.wait(5)
        assert not finished.wait(0.05) and "credential-close" not in p.calls
    finally:
        release.set()
        preparing.join(5)
        if cleaning.ident is not None:
            cleaning.join(5)
    assert not preparing.is_alive() and not cleaning.is_alive() and not failures
    assert p.calls[-1] == "credential-close"
