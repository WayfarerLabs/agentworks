"""Composition ordering with real reads and explicit synthetic start/route units.

The separate unified module exercises the actual start and route implementations.
Guest preparation here is a stub, including its real fresh locator callback.
"""

from __future__ import annotations

import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope, VMStatus
from agentworks.errors import ConfigError, LimitExceededError, StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.operations import OperationOwner
from agentworks.plugins.gcp import _native_execution_access as native
from agentworks.plugins.gcp._activation import ActivationState
from agentworks.plugins.gcp._native_access import INSTANCE_FIELDS, NATIVE_INSTANCE_FIELDS
from agentworks.plugins.gcp.platform import GCEPlatform
from agentworks.vms.target_preparation import (
    VMTargetPreparation,
    VMTargetPreparationControlFact,
    VMTargetPreparationFailure,
    VMTargetPreparationStatus,
)
from tests.plugins.gcp.test_native_identity import native_selection
from tests.plugins.gcp.test_native_instance import native_instance
from tests.plugins.gcp.test_owned_read_sdk import sdk as sdk
from tests.vms.native_stages import prepare_native


class Control(BaseException):
    pass


@pytest.fixture
def full(sdk, tmp_path, monkeypatch):
    from agentworks.plugins.gcp import _native_route

    _, vm = native_selection()
    vm.site, vm.admin_username = "site", "root"
    platform = GCEPlatform("site", {"project_id": "project", "zone": "us-central1-a"})
    settings = SSHSettings(tmp_path / "trust", tmp_path / "identity")
    ctx = SimpleNamespace(config=SimpleNamespace(operator=SimpleNamespace(ssh=settings, ssh_allow_cidrs=[])))
    p = SimpleNamespace(
        vm=vm,
        platform=platform,
        ctx=ctx,
        sdk=sdk,
        calls=[],
        current=native_instance(),
        startup=[],
        monitor=[ActivationState.SUCCEEDED],
        start_state=ActivationState.PENDING,
        start_error=None,
        start_cleanup=False,
        route_cleanup=False,
        route_settled=True,
        route_close_error=None,
        route_settle_error=None,
        start_settle_error=None,
        prefix_error=None,
        prep_error=None,
        route_hook=lambda: None,
        source_hook=lambda: None,
        guest_hook=lambda: None,
        auth_closed=False,
        prep=VMTargetPreparation(
            VMTargetPreparationStatus.PREPARED,
            ManagedTargetIdentity(
                ManagedTargetKind.VM, vm.name, "v1:" + "0" * 64, "00000000-0000-0000-0000-000000000000"
            ),
            None,
        ),
    )

    class Start:
        cleanup_incomplete = False
        closed = False

        def __init__(self, owner, name, auth, project, zone, instance, incarnation, locator):
            assert owner is p.owner and auth is p.access._auth is p.access._reader._auth
            assert (project, zone, instance, incarnation) == ("project", "us-central1-a", "backend", "123")
            assert locator.token == "gcp-gce:project:us-central1-a:123" and name == vm.name
            self.state = ActivationState.UNKNOWN
            self.obligation = None

        def start(self, deadline):
            assert p.access._start is self and deadline is p.deadline
            p.calls.append("start-post")
            self.obligation = p.owner.register_lifecycle_obligation("synthetic-start", payload_version=1, payload=b"s")
            self.obligation.mark_possible_effect()
            if p.start_error is not None:
                raise p.start_error
            self.state = p.start_state
            self.cleanup_incomplete = p.start_cleanup
            p.current["status"] = "RUNNING"

        def observe(self, deadline):
            assert not self.closed and not self.cleanup_incomplete
            assert deadline is p.deadline or deadline is p.cleanup_deadline
            p.calls.append("start-monitor")
            self.state = p.monitor.pop(0) if p.monitor else ActivationState.PENDING

        def close(self, deadline):
            p.calls.append("start-close")
            self.closed = True
            self.cleanup_incomplete = p.start_cleanup
            return not self.cleanup_incomplete

        def settle(self, deadline):
            p.calls.append("start-settle")
            self.cleanup_incomplete = p.start_cleanup
            if p.start_settle_error is not None:
                raise p.start_settle_error
            if self.cleanup_incomplete:
                return False
            if self.state in (ActivationState.SUCCEEDED, ActivationState.FAILED):
                assert self.obligation is not None
                self.obligation.resolve()
                return True
            return False

    class Route:
        cleanup_incomplete = False

        def __init__(self, owner, name, auth, identity, prefixes):
            assert owner is p.owner and auth is p.access._auth is p.access._reader._auth
            assert identity is p.access._identity and prefixes == ("192.0.2.1/32",)
            self.obligation = None

        def open(self, deadline, *, observed_instance):
            assert p.access._route is self and observed_instance.tags == tuple(p.current["tags"]["items"])
            p.calls.append("route-open")
            self.obligation = p.owner.register_lifecycle_obligation("synthetic-route", payload_version=1, payload=b"r")
            self.obligation.mark_possible_effect()
            self.cleanup_incomplete = p.route_cleanup
            p.route_hook()

        def check(self, deadline, *, observed_instance):
            assert observed_instance.endpoint == p.access._endpoint
            p.calls.append("route-check")

        def close(self, deadline):
            p.calls.append("route-close")
            self.cleanup_incomplete = p.route_cleanup
            if p.route_close_error is not None:
                raise p.route_close_error
            return not self.cleanup_incomplete

        def settle(self, deadline):
            p.calls.append("route-settle")
            if p.route_settle_error is not None:
                raise p.route_settle_error
            if p.route_settled and not self.cleanup_incomplete:
                assert self.obligation is not None
                self.obligation.resolve()
                return True
            return False

    class Source:
        def read(self, limit):
            assert limit == 65 and p.access._exposure.cleanup_incomplete
            p.calls.append("source-read")
            p.source_hook()
            return b"192.0.2.1\n"

        def close(self):
            p.calls.append("source-close")
            if p.prefix_error is not None:
                raise p.prefix_error

    def guest(vm, platform, locator, binding, *, deadline, owner, observe_locator):
        assert owner is p.owner and binding is p.access.binding and p.access.route_check is not None
        p.calls.append("guest-stub")
        assert observe_locator(deadline) == locator
        p.guest_hook()
        if p.prep_error is not None:
            raise p.prep_error
        return p.prep

    def pause(seconds):
        assert 0 < seconds <= 0.25
        sdk.now += seconds

    monkeypatch.setattr(native, "GCEActivation", Start)
    monkeypatch.setattr(_native_route, "GCENativeRoute", Route)
    monkeypatch.setattr("agentworks.vms._native_operation.prepare_managed_vm_target_from_platform", guest)
    monkeypatch.setattr(native, "time", SimpleNamespace(sleep=pause))
    monkeypatch.setattr("agentworks.vms._ssh_native_exposure.urlopen", lambda *a, **k: Source())
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent.toml")
    with closing(Database(tmp_path / "composition.db")) as database:
        p.database = database
        p.owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, vm.name), "composition-unit"
        )
        p.deadline, p.cleanup_deadline = Deadline.after(2), None
        p.access = native.GCEOwnedAccess(vm, platform, ctx, owner=p.owner)
        original_read = p.access._reader.observe_instance
        original_availability = p.access._reader.observe_availability
        original_close = p.access._reader.close

        def update(fields: str) -> None:
            if p.access._start is not None and p.startup:
                state, endpoint = p.startup.pop(0)
                p.current["status"] = state
                config = p.current["networkInterfaces"][0]["accessConfigs"][0]
                config.pop("natIP", None)
                if endpoint is not None:
                    config["natIP"] = endpoint
            sdk.fields, sdk.body = fields, json.dumps(p.current).encode()

        def observe(deadline, *, identity):
            p.calls.append("instance-read")
            update(NATIVE_INSTANCE_FIELDS)
            return original_read(deadline, identity=identity)

        def availability(deadline):
            update(INSTANCE_FIELDS)
            return original_availability(deadline)

        def close(deadline):
            p.calls.append("auth-close")
            assert not p.owner.list_pending_lifecycle_obligations()
            return original_close(deadline)

        monkeypatch.setattr(p.access._reader, "observe_instance", observe)
        monkeypatch.setattr(p.access._reader, "observe_availability", availability)
        monkeypatch.setattr(p.access._reader, "close", close)
        yield p


def test_passive_factory_exact_claim_site_and_snapshot(full):
    p = full
    access = p.platform.build_native_execution_access(p.vm, p.ctx, owner=p.owner, custody=LocalDeliveryCustody())
    assert isinstance(access, native.GCEOwnedAccess) and p.sdk.adc_calls == [] and p.sdk.sends == []
    assert access.binding is None and access.guest_check is None and access.route_check is None
    p.vm.platform_metadata["instance_id"] = "124"
    access.native_configuration_check()
    assert access._identity is not None
    assert access._identity.instance_id == "123"
    p.vm.site = "foreign"
    with pytest.raises(ValidationError):
        p.platform.build_native_execution_access(p.vm, p.ctx, owner=p.owner, custody=LocalDeliveryCustody())
    p.vm.site, p.vm.name = "site", "foreign"
    with pytest.raises(ValidationError):
        p.platform.build_native_execution_access(p.vm, p.ctx, owner=p.owner, custody=LocalDeliveryCustody())
    assert p.sdk.sends == []


@pytest.mark.parametrize("power", [VMStatus.RUNNING, VMStatus.STOPPED])
def test_availability_needs_no_native_topology_nat_ingress_or_guest(full, monkeypatch, power):
    p = full
    p.current["status"] = "RUNNING" if power is VMStatus.RUNNING else "TERMINATED"
    p.current.pop("networkInterfaces")
    p.current.pop("tags")
    p.current.pop("serviceAccounts")
    p.ctx.config.operator.ssh = None

    def deny(*args, **kwargs):
        pytest.fail("availability selected native delivery")

    monkeypatch.setattr(p.access._reader, "observe_instance", deny)
    monkeypatch.setattr(native, "ssh_native_binding", deny)
    monkeypatch.setattr(native, "OwnedSSHExposure", deny)
    monkeypatch.setattr("agentworks.vms._native_operation.prepare_managed_vm_target_from_platform", deny)
    locator = p.access.ensure_available(power, p.deadline)
    assert p.access.observe_locator(p.deadline) == locator
    assert p.access.binding is None and p.access._route is None and p.access._exposure is None
    assert "guest-stub" not in p.calls and "source-read" not in p.calls
    assert p.calls.count("start-post") == int(power is VMStatus.STOPPED)
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize("power", [VMStatus.RUNNING, VMStatus.STOPPED])
def test_success_orders_refresh_reentry_route_and_auth_last(full, power):
    p = full
    p.current["status"] = "RUNNING" if power is VMStatus.RUNNING else "TERMINATED"
    prepare_native(p, power, p.deadline)
    assert p.access.binding._independent_availability is _IndependentJobAvailability.NO_IDLE_STOP
    assert p.calls.count("start-post") == int(power is VMStatus.STOPPED)
    assert p.calls.index("source-close") < p.calls.index("route-open") < p.calls.index("guest-stub")
    assert p.calls[p.calls.index("route-open") - 1] == "instance-read"
    assert p.calls[p.calls.index("guest-stub") - 1] == "route-check"
    check = p.access.route_check
    assert check is not None
    check(p.deadline)
    assert p.access.settle(p.deadline) and p.calls[-1] == "auth-close"
    calls = list(p.calls)
    assert p.access.settle(p.deadline) and p.calls == calls
    with pytest.raises(StateError):
        check(p.deadline)
    with pytest.raises(StateError):
        prepare_native(p, power, p.deadline)


def test_availability_accepts_provider_metadata_without_native_network_selection(full):
    p = full
    p.vm.platform_metadata = {
        key: p.vm.platform_metadata[key] for key in ("project_id", "zone", "instance_name", "instance_id")
    }
    p.access = native.GCEOwnedAccess(p.vm, p.platform, p.ctx, owner=p.owner)
    p.sdk.fields, p.sdk.body = INSTANCE_FIELDS, json.dumps(p.current).encode()
    locator = p.access.ensure_available(VMStatus.RUNNING, p.deadline)
    assert p.access.observe_locator(p.deadline) == locator
    sends = len(p.sdk.sends)
    with pytest.raises(StateError):
        p.access.native_configuration_check()
    assert len(p.sdk.sends) == sends and p.calls == []
    assert p.access.settle(p.deadline)


def test_native_preflight_requires_ssh_before_provider_work(full):
    p = full
    p.ctx.config.operator.ssh = None
    with pytest.raises(ConfigError):
        prepare_native(p, VMStatus.RUNNING, p.deadline)
    assert p.sdk.sends == [] and p.sdk.adc_calls == [] and p.calls == []
    assert p.access.settle(p.deadline)


def test_power_uses_six_field_projection(full):
    p = full
    p.sdk.fields, p.sdk.body = INSTANCE_FIELDS, json.dumps(p.current).encode()
    assert p.access.observe_power(p.deadline) is VMStatus.RUNNING
    assert len(p.sdk.sends) == 1 and p.calls == []
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize("power", [VMStatus.DEALLOCATED, VMStatus.UNKNOWN, "running", None])
def test_unsupported_power_refuses_before_provider_work(full, power):
    with pytest.raises(StateError):
        prepare_native(full, power, full.deadline)
    assert full.sdk.sends == []


def test_changed_power_before_start_refuses_without_effect(full):
    with pytest.raises(StateError):
        prepare_native(full, VMStatus.STOPPED, full.deadline)
    assert "start-post" not in full.calls and "source-read" not in full.calls
    assert full.access.settle(full.deadline)


@pytest.mark.parametrize("stage", ["source", "route", "check"])
def test_fresh_endpoint_and_power_refused_before_next_boundary(full, stage):
    p = full

    def change():
        p.current["networkInterfaces"][0]["accessConfigs"][0]["natIP"] = "192.0.2.99"

    if stage == "source":
        p.source_hook = change
    elif stage == "route":
        p.route_hook = change
    else:
        prepare_native(p, VMStatus.RUNNING, p.deadline)
        p.current["status"] = "STOPPING"
    with pytest.raises(StateError):
        if stage == "check":
            p.access.route_check(p.deadline)
        else:
            prepare_native(p, VMStatus.RUNNING, p.deadline)
    assert ("route-open" in p.calls) is (stage != "source")
    assert ("guest-stub" in p.calls) is (stage == "check")
    assert p.access.settle(p.deadline)


def test_source_discovery_refreshes_all_tags_before_route(full):
    p = full
    p.source_hook = lambda: p.current["tags"]["items"].append("new-tag")
    prepare_native(p, VMStatus.RUNNING, p.deadline)
    assert p.access.settle(p.deadline)


def test_startup_states_and_delayed_nat_allocation(full):
    p = full
    p.current["status"] = "TERMINATED"
    p.startup = [(state, None) for state in ("TERMINATED", "PROVISIONING", "STAGING", "RUNNING")]
    p.startup.append(("RUNNING", "192.0.2.17"))
    prepare_native(p, VMStatus.STOPPED, p.deadline)
    assert not p.startup and p.calls.count("start-post") == 1 and p.sdk.now == 1.75
    assert p.access.settle(p.deadline)


@pytest.mark.parametrize("state", ["STOPPING", "SUSPENDED", "STOPPED", "UNKNOWN", None])
def test_invalid_post_start_state_refuses_without_more_effects(full, state):
    p = full
    p.current["status"] = "TERMINATED"
    p.startup = [(state, None)]
    with pytest.raises(StateError):
        prepare_native(p, VMStatus.STOPPED, p.deadline)
    assert p.calls.count("start-post") == 1 and "route-open" not in p.calls
    assert p.access.settle(p.deadline)


def test_losing_running_after_missing_nat_refuses(full):
    p = full
    p.current["status"] = "STOPPED"
    p.startup = [("RUNNING", None), ("STAGING", None)]
    with pytest.raises(StateError):
        prepare_native(p, VMStatus.STOPPED, p.deadline)
    assert p.calls.count("start-post") == 1 and p.access.settle(p.deadline)


def test_pending_monitor_uses_fresh_cleanup_budget_before_terminal_close(full):
    p = full
    p.current["status"], p.monitor = "TERMINATED", []
    with pytest.raises(LimitExceededError):
        prepare_native(p, VMStatus.STOPPED, p.deadline)
    p.cleanup_deadline = Deadline.after(2)
    p.monitor = [ActivationState.PENDING, ActivationState.SUCCEEDED]
    assert p.access.settle(p.cleanup_deadline)
    assert p.calls.count("start-post") == 1 and "guest-stub" not in p.calls
    assert p.calls.index("start-close") > max(i for i, event in enumerate(p.calls) if event == "start-monitor")


@pytest.mark.parametrize("state", [ActivationState.UNKNOWN, ActivationState.FAILED])
def test_start_failure_or_unknown_never_inferred_from_running(full, state):
    p = full
    p.current["status"], p.start_state = "TERMINATED", state
    with pytest.raises(StateError):
        prepare_native(p, VMStatus.STOPPED, p.deadline)
    assert p.access.settle(p.deadline) is (state is ActivationState.FAILED)
    assert p.calls.count("start-post") == 1 and "route-open" not in p.calls


@pytest.mark.parametrize("retained", [False, True])
def test_escaping_guest_control_retains_exact_partial_fact(full, retained):
    p = full
    p.prep = VMTargetPreparation(
        VMTargetPreparationStatus.UNCERTAIN if retained else VMTargetPreparationStatus.FAILED,
        None,
        None,
        VMTargetPreparationFailure.GUEST_OBSERVATION,
        pending_remote_effects=retained,
        requires_owner_retention=retained,
    )
    p.prep_error = Control("guest")
    p.prep_error.__cause__ = VMTargetPreparationControlFact(p.prep)
    with pytest.raises(Control) as caught:
        prepare_native(p, VMStatus.RUNNING, p.deadline)
    assert caught.value is p.prep_error and p.workflow.preparation is p.prep
    assert p.workflow._components_settled() is not retained
    assert p.access.settle(p.deadline)


def test_cleanup_preserves_first_control_and_attempts_independent_children(full):
    p = full
    p.current["status"] = "TERMINATED"
    prepare_native(p, VMStatus.STOPPED, p.deadline)
    first = Control("route-close")
    p.route_close_error = first
    p.route_settle_error = Control("route-settle")
    p.start_settle_error = RuntimeError("start")
    with pytest.raises(Control) as caught:
        p.access.settle(p.deadline)
    assert caught.value is first
    assert p.calls[-3:] == ["route-close", "route-settle", "start-settle"]
    assert "auth-close" not in p.calls and p.access.route_check is None
    p.route_close_error = p.route_settle_error = p.start_settle_error = None
    assert p.access.settle(p.deadline)


def test_auth_close_retry_does_not_revisit_resolved_children(full, monkeypatch):
    p = full
    prepare_native(p, VMStatus.RUNNING, p.deadline)
    p.sdk.failed_close = "credential-session"
    assert not p.access.settle(p.deadline)
    assert not p.owner.list_pending_lifecycle_obligations()
    calls = list(p.calls)
    p.sdk.failed_close = None
    monkeypatch.setattr(p.access._route, "settle", lambda _: pytest.fail("settled route revisited"))
    monkeypatch.setattr(p.access._reader, "close_read_client", lambda _: pytest.fail("settled reader revisited"))
    assert p.access.settle(p.deadline) and p.calls == [*calls, "auth-close"]
