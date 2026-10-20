"""Independent GCE factory composition with real ownership and scripted boundaries."""

from __future__ import annotations

import json
from contextlib import closing, contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator, ProvisionRequest
from agentworks.config import Config, DefaultsConfig, OperatorConfig, PathsConfig, SessionConfig
from agentworks.db import Database, OperationResourceKind, OperationScope, VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import AlreadyExistsError, StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.binding import NativeExecutionBinding
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.gcp import _creation_access as creation
from agentworks.plugins.gcp._creation_api import ROOT
from agentworks.plugins.gcp._creation_resources import CreationResources
from agentworks.plugins.gcp.platform import GCEPlatform
from agentworks.vms._ssh_native_exposure import OwnedSSHExposure
from tests.plugins.test_gcp_creation_resources import GLOBAL, NETWORK, ZONAL, ScriptedAPI
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY

MARKER = "a" * 32


class Progress:
    def step(self, text):
        pass

    def output(self, text):
        pass

    def warning(self, text):
        pass

    def log_error(self, text):
        pass


class ProviderAPI(ScriptedAPI):
    def get(self, path: str, deadline: Deadline, *, missing: bool = False) -> dict[str, Any] | None:
        self.calls.append(("GET", path))
        if path == "projects/project":
            return {"kind": "compute#project", "name": "project"}
        if path.startswith(GLOBAL + "/firewalls?"):
            return {"items": [dict(value) for key, value in self.live.items() if "/firewalls/" in key]}
        if path == NETWORK:
            return {"kind": "compute#network", "name": "default", "id": "12", "selfLink": ROOT + path}
        if path == ZONAL:
            return {"kind": "compute#zone", "name": "us-central1-a", "id": "13", "selfLink": ROOT + path}
        if "/machineTypes/" in path:
            return {
                "kind": "compute#machineType",
                "name": "e2-standard-2",
                "id": "14",
                "selfLink": ROOT + path,
                "guestCpus": 2,
                "memoryMb": 8192,
                "architecture": "X86_64",
                "maximumPersistentDisks": 16,
            }
        if "/images/family/" in path:
            return {
                "kind": "compute#image",
                "name": "debian-13-sample",
                "id": "15",
                "architecture": "X86_64",
                "selfLink": ROOT + "projects/debian-cloud/global/images/debian-13-sample",
            }
        if "/diskTypes/" in path:
            return {"kind": "compute#diskType", "name": "pd-balanced", "id": "16", "selfLink": ROOT + path}
        self.calls.pop()
        return super().get(path, deadline, missing=missing)

    @contextmanager
    def exchange(self, method, path, deadline, **kwargs):
        with super().exchange(method, path, deadline, **kwargs) as value:
            if method == "POST" and path.startswith(ZONAL + "/instances?"):
                instance = self.live[ZONAL + "/instances/sample"]
                instance["status"] = "RUNNING"
                instance["networkInterfaces"][0]["accessConfigs"][0]["natIP"] = "192.0.2.9"
            yield value

    def settle(self, deadline, *, final):
        assert not deadline.expired
        return True


@pytest.fixture
def owned(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "absent.toml")
    settings = SSHSettings(tmp_path / "trust", tmp_path / "identity")
    config = Config(
        OperatorConfig(tmp_path / "public", tmp_path / "private", ssh=settings),
        PathsConfig(),
        DefaultsConfig(),
        tmp_path / "absent.toml",
        SessionConfig(),
    )
    platform = GCEPlatform("offline", {"project_id": "project", "zone": "us-central1-a"})
    provision = ProvisionRequest(
        vm_name="sample",
        debian_release=DebianRelease.TRIXIE,
        hostname="sample",
        system_slug=None,
        instance_marker=MARKER,
        admin_username="operator",
        ssh_public_key=TEST_SSH_PUBLIC_KEY,
        ssh_private_key=None,
        tailscale_auth_key="join-secret-sentinel",
        progress=Progress(),
        cpus=2,
        memory_gib=8,
        disk_gib=20,
        swap_gib=0,
    )
    with closing(Database(tmp_path / "state.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "sample"), "proof")
        api = ProviderAPI(owner)
        p = SimpleNamespace(
            database=database,
            owner=owner,
            api=api,
            events=[],
            request=provision,
            platform=platform,
            ctx=RunContext(config=config),
            custody=LocalDeliveryCustody(),
            settings=settings,
            deadline=Deadline.after(30),
            clock=clock,
            fail_probe=None,
        )
        monkeypatch.setattr(creation, "CreationAPI", lambda *args: api)
        monkeypatch.setattr(OwnedSSHExposure, "resolve", lambda *args: ("192.0.2.1/32",))

        def resources(owner, api, selected):
            controller = CreationResources(owner, api, selected)
            api.controller = controller
            return controller

        class Readiness:
            def wait(self, endpoint, deadline):
                assert p.access._readiness is self and endpoint == "192.0.2.9"
                p.events.append("tcp")

            def close(self):
                p.events.append("tcp-close")
                return True

        class Trust:
            def __init__(self, selected_owner, **kwargs):
                assert selected_owner is owner
                assert kwargs["vm_name"] == "sample" and kwargs["instance_marker"] == MARKER
                assert kwargs["endpoint"] == "192.0.2.9" and kwargs["settings"] is settings

            def prepare(self):
                assert p.access._trust is self and p.events[-1] == "tcp"
                p.events.append("trust")

            def close(self, deadline):
                p.events.append("trust-close")
                return True

        def probe(connection, marker, deadline, custody, *, bootstrap_check):
            assert p.events[-1] == "trust" and marker == MARKER and custody is p.custody
            assert connection.host == "192.0.2.9" and connection.user == "operator"
            assert bootstrap_check and "join-secret-sentinel" not in bootstrap_check
            p.events.append("probe")
            if p.fail_probe is not None:
                raise p.fail_probe

        def retired(*args, **kwargs):
            pytest.fail("retired provider orchestration entered")

        for method in (
            "create",
            "build_native_execution_access",
            "resolve_native_execution_binding",
            "native_transport",
        ):
            monkeypatch.setattr(GCEPlatform, method, retired)
        monkeypatch.setattr(creation, "CreationResources", resources)
        monkeypatch.setattr(creation, "SSHReadiness", Readiness)
        monkeypatch.setattr(creation, "CreationSSHTrust", Trust)
        monkeypatch.setattr(creation, "observe_early_guest", probe)
        p.access = platform.build_creation_access(p.request, p.ctx, owner=owner, custody=p.custody)
        yield p


def test_passive_factory_then_new_result_and_accepted_retention(owned):
    p = owned
    assert not p.api.calls and not p.events and not p.owner.list_pending_lifecycle_obligations()
    result = p.access.provision(p.deadline)
    assert type(result.binding) is NativeExecutionBinding
    assert result.locator == ProviderLocator("gcp-gce:project:us-central1-a:103")
    assert p.events == ["tcp", "trust", "probe"]
    assert result.platform_metadata["boot_disk_id"] == "104"
    assert p.access.observe_power(p.deadline) is VMStatus.RUNNING
    assert p.access.observe_locator(p.deadline) == result.locator
    p.access.complete_bootstrap(p.deadline)
    p.access.request_rollback()
    assert p.access.settle(p.deadline)
    deletes = [path for method, path in p.api.calls if method == "DELETE"]
    assert len(deletes) == 1 and "/firewalls/" in deletes[0]
    assert ZONAL + "/instances/sample" in p.api.live and result.platform_metadata["boot_disk"] in p.api.live
    assert not p.owner.list_pending_lifecycle_obligations()
    assert p.database.operations.inspect(p.owner.ownership.scope) is not None
    assert p.access.platform_metadata is not result.platform_metadata


def test_public_prerequisite_failure_rolls_back_exact_unaccepted_resources(owned):
    p = owned
    primary = KeyboardInterrupt("strict probe interrupted")
    p.fail_probe = primary
    with pytest.raises(KeyboardInterrupt) as caught:
        p.access.provision(p.deadline)
    assert caught.value is primary
    p.access.request_rollback()
    assert p.access.settle(p.deadline)
    assert not p.api.live and not p.owner.list_pending_lifecycle_obligations()
    assert p.events[-2:] == ["tcp-close", "trust-close"]
    assert p.access.platform_metadata["instance_id"] == "103"


def test_partial_receipt_metadata_extends_without_changing_prior_snapshots(owned):
    p = owned
    p.api.fault = ("POST", "allow")
    with pytest.raises(KeyboardInterrupt):
        p.access.provision(p.deadline)
    before = p.access.platform_metadata
    assert before["deny_rule_id"] == "101" and "allow_rule_id" not in before
    assert "instance_id" not in before
    p.access.request_rollback()
    with pytest.raises(StateError):
        p.access.settle(p.deadline)
    assert p.access.platform_metadata == before
    assert len([method for method, _path in p.api.calls if method == "POST"]) == 2


def test_fresh_cleanup_budget_rolls_back_after_original_provision_expiry(owned):
    p = owned
    p.access.provision(p.deadline)
    p.access.request_rollback()
    p.clock[0] = 131.0
    assert p.deadline.expired
    assert p.access.settle(Deadline.after(60))
    assert not p.api.live and not p.owner.list_pending_lifecycle_obligations()
    assert len([method for method, _path in p.api.calls if method == "POST"]) == 3
    assert len([method for method, _path in p.api.calls if method == "DELETE"]) == 3


def test_retained_observations_use_fresh_cleanup_budget_after_original_expiry(owned):
    p = owned
    result = p.access.provision(p.deadline)
    route_check = p.access.route_check
    assert route_check is not None
    p.clock[0] = 131.0
    assert p.deadline.expired
    p.owner.stop_admission()
    before = len(p.api.calls)
    deadline = Deadline.after(60)
    route_check(deadline)
    assert p.access.observe_power(deadline) is VMStatus.RUNNING
    assert p.access.observe_locator(deadline) == result.locator
    observed = p.api.calls[before:]
    assert observed.count(("GET", ZONAL + "/instances/sample")) == 3
    assert ("GET", NETWORK) in observed
    assert any(method == "GET" and path.startswith(GLOBAL + "/firewalls?") for method, path in observed)
    assert all(method == "GET" for method, _path in observed)


@pytest.mark.parametrize("budget", [Deadline(130.0), Deadline(None)])
def test_invalid_supplied_budget_refuses_observation_and_transfer_before_io(owned, budget):
    p = owned
    p.access.provision(p.deadline)
    route_check = p.access.route_check
    assert route_check is not None
    p.clock[0] = 131.0
    before = list(p.api.calls)
    for action in (route_check, p.access.observe_power, p.access.observe_locator, p.access.complete_bootstrap):
        with pytest.raises((TimeoutError, ValidationError)):
            action(budget)
    assert not p.access._resources.accepted
    assert p.api.calls == before


def test_fresh_observation_still_refuses_replaced_vm_after_original_expiry(owned):
    p = owned
    p.access.provision(p.deadline)
    route_check = p.access.route_check
    assert route_check is not None
    p.clock[0] = 131.0
    p.api.live[ZONAL + "/instances/sample"]["id"] = "999"
    before = len(p.api.calls)
    for observe in (route_check, p.access.observe_power, p.access.observe_locator):
        with pytest.raises(StateError):
            observe(Deadline.after(60))
    assert p.api.calls[before:] == [("GET", ZONAL + "/instances/sample")] * 3


def test_fresh_observation_retains_exact_owner_fence_before_provider_io(owned, monkeypatch):
    p = owned
    p.access.provision(p.deadline)
    route_check = p.access.route_check
    assert route_check is not None
    p.clock[0] = 131.0
    before = list(p.api.calls)
    primary = StateError("original owner is unavailable")

    def refuse():
        raise primary

    monkeypatch.setattr(p.owner, "list_pending_lifecycle_obligations", refuse)
    for observe in (route_check, p.access.observe_power, p.access.observe_locator):
        with pytest.raises(StateError) as caught:
            observe(Deadline.after(60))
        assert caught.value is primary
    assert p.api.calls == before


@pytest.mark.parametrize("lost_reply", [False, True])
def test_fresh_cleanup_transfers_known_completion_and_removes_only_exact_allow(owned, monkeypatch, lost_reply):
    p = owned
    result = p.access.provision(p.deadline)
    p.clock[0] = 131.0
    assert p.deadline.expired
    p.owner.stop_admission()
    before = list(p.api.calls)
    with pytest.raises(StateError):
        p.access.provision(Deadline.after(60))
    assert p.api.calls == before
    original = LifecycleObligation.publish_payload

    def publish(self, **kwargs):
        original(self, **kwargs)
        if json.loads(kwargs["payload"])["accepted"]:
            raise KeyboardInterrupt("acceptance publication reply lost")

    if lost_reply:
        monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
        with pytest.raises(KeyboardInterrupt):
            p.access.complete_bootstrap(Deadline.after(60))
        assert p.access._resources.accepted
        p.access.request_rollback()
        assert not p.access._rollback
        monkeypatch.setattr(LifecycleObligation, "publish_payload", original)
        p.clock[0] = 192.0
    p.access.complete_bootstrap(Deadline.after(60))
    p.access.request_rollback()
    assert p.access.settle(Deadline.after(60))
    assert ZONAL + "/instances/sample" in p.api.live and result.platform_metadata["boot_disk"] in p.api.live
    assert len([method for method, _path in p.api.calls if method == "POST"]) == 3
    deletes = [path for method, path in p.api.calls if method == "DELETE"]
    assert deletes == [GLOBAL + "/firewalls/" + result.platform_metadata["allow_rule"]]
    assert GLOBAL + "/firewalls/" + result.platform_metadata["deny_rule"] in p.api.live
    assert not p.owner.list_pending_lifecycle_obligations()


def test_fresh_cleanup_observes_original_pending_receipt_without_insert_replay(owned, monkeypatch):
    p = owned
    p.api.insert_pending = True
    original = p.api.exchange
    primary = KeyboardInterrupt("original operation observation")
    interrupted = False

    @contextmanager
    def exchange(method, path, deadline, **kwargs):
        nonlocal interrupted
        if method == "GET" and path.endswith("/operations/Insert_instance") and not interrupted:
            interrupted = True
            raise primary
        with original(method, path, deadline, **kwargs) as value:
            yield value

    monkeypatch.setattr(p.api, "exchange", exchange)
    with pytest.raises(KeyboardInterrupt) as caught:
        p.access.provision(p.deadline)
    assert caught.value is primary
    instance = p.access._resources.resources["instance"]
    assert instance.create_operation == "Insert_instance" and instance.resource_id == "103"
    p.clock[0] = 131.0
    assert p.deadline.expired
    p.access.request_rollback()
    assert p.access.settle(Deadline.after(60))
    assert not p.api.live and not p.owner.list_pending_lifecycle_obligations()
    assert len([method for method, _path in p.api.calls if method == "POST"]) == 3
    assert ("GET", ZONAL + "/operations/Insert_instance") in p.api.calls


def test_creation_public_payload_and_durable_intent_exclude_join_secret(owned):
    p = owned
    p.access.provision(p.deadline)
    for row in p.owner.list_pending_lifecycle_obligations():
        assert b"join-secret-sentinel" not in row.payload
    assert p.access._request.tailscale_auth_key == ""
    with pytest.raises(StateError):
        p.access.provision(Deadline.after(60))


def test_creation_rejects_different_vm_owner_before_effects(owned):
    p = owned
    with pytest.raises(ValidationError):
        p.platform.build_creation_access(replace(p.request, vm_name="other"), p.ctx, owner=p.owner, custody=p.custody)
    assert not p.api.calls


def test_known_disposition_bookkeeping_can_finish_after_original_budget(owned, monkeypatch):
    p = owned
    p.access.provision(p.deadline)
    p.access.complete_bootstrap(p.deadline)
    resources = p.access._resources
    original = resources.resolve
    primary = KeyboardInterrupt("final local bookkeeping")
    fired = False

    def resolve(deadline):
        nonlocal fired
        if not fired:
            fired = True
            raise primary
        return original(deadline)

    monkeypatch.setattr(resources, "resolve", resolve)
    with pytest.raises(KeyboardInterrupt) as caught:
        p.access.settle(p.deadline)
    assert caught.value is primary and resources.disposition_complete
    provider_calls = list(p.api.calls)
    p.clock[0] = 131.0
    assert p.deadline.expired
    assert p.access.settle(Deadline.after(60))
    assert p.api.calls == provider_calls


def test_predictable_vm_name_disk_is_never_selected_or_attached(owned):
    p = owned
    predictable = ZONAL + "/disks/sample"
    prior = {"kind": "compute#disk", "name": "sample", "id": "999", "selfLink": ROOT + predictable}
    p.api.live[predictable] = dict(prior)
    result = p.access.provision(p.deadline)
    resources = p.access._resources.resources
    disk_name = resources["instance"].body["disks"][0]["initializeParams"]["diskName"]
    assert disk_name != "sample" and disk_name == p.access._disk_name
    assert result.platform_metadata["boot_disk"] == ZONAL + "/disks/" + disk_name
    assert result.platform_metadata["boot_disk_id"] == "104"
    p.access.request_rollback()
    assert p.access.settle(p.deadline)
    assert p.api.live == {predictable: prior}


def test_exact_random_boot_disk_collision_refuses_before_mutation(owned):
    p = owned
    selected = ZONAL + "/disks/" + p.access._disk_name
    p.api.live[selected] = {
        "kind": "compute#disk",
        "name": p.access._disk_name,
        "id": "999",
        "selfLink": ROOT + selected,
    }
    with pytest.raises(AlreadyExistsError):
        p.access.provision(p.deadline)
    assert not any(method in ("POST", "DELETE") for method, _path in p.api.calls)
    assert not p.owner.list_pending_lifecycle_obligations()


@pytest.mark.parametrize(
    "source,denied,port,refused",
    [
        ("2001:db8::/64", False, "80", False),
        ("2001:db8::/64", True, "22", False),
        ("192.0.2.0/24", True, "22", True),
    ],
)
def test_creation_policy_ignores_irrelevant_ipv6_and_refuses_applicable_ipv4_ssh_deny(
    owned, source, denied, port, refused
):
    p = owned
    path = GLOBAL + "/firewalls/other-rule"
    rule = {
        "kind": "compute#firewall",
        "name": "other-rule",
        "id": "999",
        "selfLink": ROOT + path,
        "network": NETWORK,
        "direction": "INGRESS",
        "priority": 1000,
        "sourceRanges": [source],
        "denied" if denied else "allowed": [{"IPProtocol": "tcp", "ports": [port]}],
    }
    p.api.live[path] = rule
    if refused:
        with pytest.raises(StateError):
            p.access.provision(p.deadline)
        assert not any(method == "POST" for method, _path in p.api.calls)
    else:
        p.access.provision(p.deadline)
        p.access.request_rollback()
        assert p.access.settle(p.deadline)
    assert p.api.live == {path: rule}
