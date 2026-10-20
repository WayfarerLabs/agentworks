"""Passive factory ownership, partial metadata and immutable Azure acceptance."""

from __future__ import annotations

import base64
import copy
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator, ProvisionRequest
from agentworks.config import Config, DefaultsConfig, OperatorConfig, PathsConfig, SessionConfig
from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.debian import DebianRelease
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.operations import LifecycleObligation, OperationOwner
from agentworks.plugins.azure import _route_client
from agentworks.plugins.azure._creation_access import AzureOwnedCreationAccess
from agentworks.plugins.azure._creation_request import AzureCreationRequests, CreationDeadlineExpired, resource_url
from agentworks.plugins.azure._native_route_evidence import RuleAcknowledgment
from agentworks.plugins.azure._owned_auth import _OwnedAzureCredential
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.vms._creation_access import VMCreationResult
from agentworks.vms._ssh_native_binding import ssh_execution_binding
from tests.plugins.azure.test_activation_sdk import Credential, deny
from tests.plugins.azure.test_native_route import RuleResponse
from tests.plugins.test_azure_creation_requests import Transport
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY

SUB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
VM_ID = "11234567-89ab-0cde-0123-456789abcdef"
_ARM_CALL = AzureCreationRequests.call


class UnusedProgress:
    def step(self, name: str) -> None:
        raise AssertionError(name)

    def output(self, text: str) -> None:
        raise AssertionError(text)

    def warning(self, msg: str) -> None:
        raise AssertionError(msg)

    def log_error(self, msg: str) -> None:
        raise AssertionError(msg)


@dataclass
class FactoryHarness:
    access: AzureOwnedCreationAccess
    owner: OperationOwner
    platform: AzureVMPlatform
    request: ProvisionRequest
    context: RunContext
    database: Database


@pytest.fixture
def owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FactoryHarness]:
    monkeypatch.setattr(_OwnedAzureCredential, "credential", deny)
    monkeypatch.setattr(AzureCreationRequests, "call", deny)
    platform = AzureVMPlatform(
        "site",
        {
            "name": "azure-vm",
            "subscription_id": SUB,
            "resource_group": "group",
            "region": "eastus",
        },
    )
    request = ProvisionRequest(
        vm_name="vm",
        debian_release=DebianRelease.TRIXIE,
        hostname="project-vm",
        system_slug="project",
        instance_marker="a" * 32,
        admin_username="operator",
        ssh_public_key=TEST_SSH_PUBLIC_KEY,
        ssh_private_key=None,
        tailscale_auth_key="join-secret-sentinel",
        progress=UnusedProgress(),
        cpus=2,
        memory_gib=4,
        disk_gib=40,
        swap_gib=0,
    )
    config = Config(
        OperatorConfig(
            tmp_path / "public", tmp_path / "private", ssh=SSHSettings(tmp_path / "trust", tmp_path / "identity")
        ),
        PathsConfig(),
        DefaultsConfig(),
        tmp_path / "absent.toml",
        SessionConfig(),
    )
    context = RunContext(config=config)
    with closing(Database(tmp_path / "factory.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm"), "proof")
        access = platform.build_creation_access(request, context, owner=owner, custody=LocalDeliveryCustody())
        assert isinstance(access, AzureOwnedCreationAccess)
        yield FactoryHarness(
            access=access,
            owner=owner,
            platform=platform,
            request=request,
            context=context,
            database=database,
        )


def test_factory_is_passive_and_public_configuration_has_no_join_secret(owned: FactoryHarness) -> None:
    access = owned.access
    assert access.binding is None and dict(access.platform_metadata) == {}
    assert owned.owner.list_pending_lifecycle_obligations() == ()
    vm = access._requests.plan[-1]
    startup = base64.b64decode(vm.body["properties"]["osProfile"]["customData"])
    assert owned.request.tailscale_auth_key.encode() not in startup
    assert not access._auth.has_credential
    assert access.settle(Deadline.after(2))


def test_factory_refuses_foreign_owner_before_effects(owned: FactoryHarness) -> None:
    owned.request.vm_name = "other"
    with pytest.raises(ValidationError):
        owned.platform.build_creation_access(
            owned.request,
            owned.context,
            owner=owned.owner,
            custody=LocalDeliveryCustody(),
        )


def test_partial_receipts_publish_addressability_without_unobserved_vm_id(owned: FactoryHarness) -> None:
    resources = owned.access._requests.resources
    resources[0].create_ack = RuleAcknowledgment(201, None, None, None)
    snapshot = owned.access.platform_metadata
    assert snapshot == {"public_ip_resource_id": resources[0].resource_id}
    resources[-1].create_ack = RuleAcknowledgment(201, None, None, None)
    metadata = owned.access.platform_metadata
    assert metadata["resource_id"] == resources[-1].resource_id and "vm_id" not in metadata
    resources[-1].identity = VM_ID
    assert owned.access.platform_metadata["vm_id"] == VM_ID
    assert snapshot == {"public_ip_resource_id": resources[0].resource_id}


def test_acceptance_never_reopens_rollback_even_after_publication_failure(
    owned: FactoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    access = owned.access
    access._result = VMCreationResult(
        {},
        ssh_execution_binding(access._vm, "192.0.2.10", access._settings),
        ProviderLocator("creation-test"),
        ssh_trust_published=True,
    )
    monkeypatch.setattr(access._requests, "reconcile", lambda deadline: (_ for _ in ()).throw(OSError("lost reply")))
    with pytest.raises(OSError):
        access.complete_bootstrap(Deadline.after(2))
    with pytest.raises(StateError):
        access.request_rollback()


@pytest.mark.parametrize("budget", [Deadline(105.0), Deadline(None)])
def test_invalid_supplied_acceptance_budget_refuses_before_publication(
    owned: FactoryHarness, monkeypatch: pytest.MonkeyPatch, budget: Deadline
) -> None:
    access = owned.access
    access._result = VMCreationResult(
        {},
        ssh_execution_binding(access._vm, "192.0.2.10", access._settings),
        ProviderLocator("creation-test"),
        ssh_trust_published=True,
    )
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: 110.0)

    def refuse(deadline: Deadline) -> bool:
        pytest.fail("invalid budget entered acceptance publication")

    monkeypatch.setattr(access._requests, "reconcile", refuse)
    with pytest.raises((CreationDeadlineExpired, ValidationError)):
        access.complete_bootstrap(budget)
    assert not access._requests.accepted


@pytest.mark.parametrize("lost_reply", [False, True])
def test_public_route_readiness_trust_and_probe_precede_binding_and_transfer(
    owned: FactoryHarness, monkeypatch: pytest.MonkeyPatch, lost_reply: bool
) -> None:
    from agentworks.plugins.azure import _creation_access as module
    from agentworks.vms import _creation_ssh

    access = owned.access
    requests = access._requests
    models: dict[str, dict[str, Any]] = {}
    events: list[str] = []
    credential = object()
    clock = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: clock[0])
    deadline = Deadline.after(10)
    creates: list[int] = []
    monkeypatch.setattr(_OwnedAzureCredential, "credential", lambda self, budget: credential)

    def create(index: int, selected: object, budget: Deadline) -> dict[str, Any]:
        creates.append(index)
        assert selected is credential and budget is deadline
        assert owned.owner.list_pending_lifecycle_obligations()
        if not requests._marked:
            requests._marked = True
            row = owned.owner.inspect_lifecycle_obligation(requests._id)
            assert row is not None
            LifecycleObligation(owned.owner, row).mark_possible_effect()
        item = requests.resources[index]
        item.sent = True
        item.create_ack = RuleAcknowledgment(201, None, None, None)
        item.create_state, item.identity = "Succeeded", VM_ID
        model = copy.deepcopy(requests.plan[index].body)
        model["id"] = item.resource_id
        props = model["properties"]
        props.update({"provisioningState": "Succeeded", "resourceGuid": VM_ID})
        if index == 0:
            props["ipAddress"] = "192.0.2.10"
        if index == 4:
            props["vmId"] = VM_ID
            props["instanceView"] = {"statuses": [{"code": "PowerState/running"}]}
            props["storageProfile"]["osDisk"]["managedDisk"]["id"] = requests.disk_id
            requests.observed_disk_id = requests.disk_id
        models[item.resource_id] = model
        requests.reconcile(budget)
        return model

    monkeypatch.setattr(requests, "create", create)
    monkeypatch.setattr(requests, "get", lambda selected, resource, budget, **kwargs: models.get(resource))
    monkeypatch.setattr(access._exposure, "resolve", lambda cidrs, budget: ("192.0.2.1/32",))

    class Route:
        def __init__(self, *args: object) -> None:
            self.cleanup_incomplete = False

        def open(self, budget: Deadline) -> None:
            assert id(access._route) == id(self) and budget is deadline
            events.append("route")

        def check(self, budget: Deadline, *, observed_vm: Any) -> None:
            assert observed_vm.vm_id == VM_ID and budget is deadline

        def close(self, budget: Deadline) -> bool:
            return True

        def settle(self, budget: Deadline) -> bool:
            return True

    class Ready:
        def wait(self, endpoint: str, budget: Deadline) -> None:
            assert id(access._readiness) == id(self) and endpoint == "192.0.2.10" and budget is deadline
            events.append("listening")

        def close(self) -> bool:
            return True

    class Trust:
        def __init__(self, owner: OperationOwner, **kwargs: Any) -> None:
            assert owner is owned.owner and kwargs["instance_marker"] == owned.request.instance_marker

        def prepare(self) -> None:
            assert id(access._trust) == id(self)
            events.append("trust")

        def close(self, budget: Deadline) -> bool:
            return True

    def probe(
        connection: Any,
        marker: str,
        budget: Deadline,
        custody: LocalDeliveryCustody,
        *,
        bootstrap_check: str,
    ) -> None:
        assert connection.host == "192.0.2.10" and marker == owned.request.instance_marker
        assert budget is deadline and custody is access._custody and bootstrap_check
        events.append("probe")

    monkeypatch.setattr(module, "AzureNativeRoute", Route)
    monkeypatch.setattr(module, "CreationSSHTrust", Trust)
    monkeypatch.setattr(_creation_ssh, "SSHReadiness", Ready)
    monkeypatch.setattr(_creation_ssh, "observe_early_guest", probe)
    result = access.provision(deadline)
    assert result.ssh_trust_published is True
    assert events == ["route", "listening", "trust", "probe"]
    assert result.binding is access.binding and result.binding.delivery_account == "operator"
    assert result.platform_metadata["vm_id"] == VM_ID
    assert result.locator.token == f"azure-vm:v2:{VM_ID}:{requests.resources[-1].resource_id}"
    clock[0] = 111.0
    assert deadline.expired
    owned.owner.stop_admission()
    fresh = Deadline.after(10)
    with pytest.raises(StateError):
        access.provision(fresh)
    original = LifecycleObligation.publish_payload

    def publish(self: LifecycleObligation, **kwargs: Any) -> None:
        original(self, **kwargs)
        raise KeyboardInterrupt("acceptance publication reply lost")

    if lost_reply:
        monkeypatch.setattr(LifecycleObligation, "publish_payload", publish)
        with pytest.raises(KeyboardInterrupt):
            access.complete_bootstrap(fresh)
        assert requests.accepted
        with pytest.raises(StateError):
            access.request_rollback()
        monkeypatch.setattr(LifecycleObligation, "publish_payload", original)
        clock[0] = 122.0
        fresh = Deadline.after(10)
    access.complete_bootstrap(fresh)
    with pytest.raises(StateError):
        access.request_rollback()
    assert access.settle(fresh)
    assert creates == list(range(5))
    assert all(not item.delete_sent and not item.absent for item in requests.resources)
    assert owned.owner.list_pending_lifecycle_obligations() == ()


@dataclass
class FailedCreation:
    transport: Transport
    clock: list[float]
    auth_closes: list[str]


def failed_creation(owned: FactoryHarness, monkeypatch: pytest.MonkeyPatch) -> FailedCreation:
    """Run actual resource creation, then fail before public guest preparation."""
    from agentworks.plugins.azure import _creation_access as module

    transport = Transport()
    credential = Credential()
    clock = [100.0]
    auth_closes: list[str] = []
    original_client = _route_client.network_route_client
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(AzureCreationRequests, "call", _ARM_CALL)
    monkeypatch.setattr(
        _route_client,
        "network_route_client",
        lambda selected, subscription: original_client(selected, subscription, transport=transport),
    )
    monkeypatch.setattr(owned.access._auth, "credential", lambda budget: credential)
    monkeypatch.setattr(owned.access._exposure, "resolve", lambda cidrs, budget: ("192.0.2.1/32",))

    def close_credential() -> bool:
        auth_closes.append("closed")
        return True

    monkeypatch.setattr(owned.access._auth, "close", close_credential)

    class Route:
        def __init__(self, *args: object) -> None:
            pass

        def open(self, deadline: Deadline) -> None:
            raise StateError("Synthetic public guest preparation failure")

        def close(self, deadline: Deadline) -> bool:
            return True

        def settle(self, deadline: Deadline) -> bool:
            return True

    monkeypatch.setattr(module, "AzureNativeRoute", Route)
    with pytest.raises(StateError):
        owned.access.provision(Deadline(105.0))
    assert len([request for request in transport.calls if request.method == "PUT"]) == 5
    assert all(resource.create_state == "Succeeded" for resource in owned.access._requests.resources)
    return FailedCreation(transport, clock, auth_closes)


def test_fresh_cleanup_after_provisioning_expiry_uses_originals_without_replay(
    owned: FactoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = failed_creation(owned, monkeypatch)
    failed.clock[0] = 110.0
    with pytest.raises(StateError):
        owned.access.provision(Deadline(120.0))
    owned.access.request_rollback()
    assert owned.access.settle(Deadline(120.0))
    assert not failed.transport.values and failed.auth_closes == ["closed"]
    mutations = [request.method for request in failed.transport.calls if request.method != "GET"]
    assert mutations == ["PUT"] * 5 + ["DELETE"] * 5
    assert owned.owner.list_pending_lifecycle_obligations() == ()
    requests_before = len(failed.transport.calls)
    assert owned.access.settle(Deadline(130.0))
    assert len(failed.transport.calls) == requests_before


def test_last_http_close_failure_retains_ledger_and_credentials_for_fresh_cleanup(
    owned: FactoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = failed_creation(owned, monkeypatch)
    failed.clock[0] = 110.0
    disk_url = resource_url(owned.access._requests.disk_id)

    def fail_disk_close(response: RuleResponse) -> None:
        if response.request.url == disk_url and response.status_code == 404:
            response.close_errors = [OSError("synthetic original close failure")] * 2

    failed.transport.on_response = fail_disk_close
    owned.access.request_rollback()
    assert not owned.access.settle(Deadline(120.0))
    assert owned.access._requests.cleanup_incomplete and not owned.access._settled
    assert owned.owner.list_pending_lifecycle_obligations() and not failed.auth_closes
    mutations = [request.method for request in failed.transport.calls if request.method != "GET"]
    failed.transport.on_response = None
    assert owned.access.settle(Deadline(130.0))
    assert not owned.access._requests.cleanup_incomplete
    assert owned.owner.list_pending_lifecycle_obligations() == ()
    assert failed.auth_closes == ["closed"]
    assert [request.method for request in failed.transport.calls if request.method != "GET"] == mutations


def test_unsettled_reconciliation_cannot_report_complete_or_close_credentials(
    owned: FactoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = failed_creation(owned, monkeypatch)
    owned.access.request_rollback()
    original = owned.access._requests.reconcile
    attempts: list[bool] = []

    def reconcile(deadline: Deadline, *, resolve: bool = False) -> bool:
        if resolve and not attempts:
            attempts.append(True)
            return False
        return original(deadline, resolve=resolve)

    monkeypatch.setattr(owned.access._requests, "reconcile", reconcile)
    assert not owned.access.settle(Deadline(120.0))
    assert not owned.access._settled and not failed.auth_closes
    assert owned.owner.list_pending_lifecycle_obligations()
    assert owned.access.settle(Deadline(130.0))
    assert failed.auth_closes == ["closed"] and owned.owner.list_pending_lifecycle_obligations() == ()
