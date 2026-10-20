"""Passive factory ownership, partial metadata and immutable Azure acceptance."""

from __future__ import annotations

import base64
import copy
from contextlib import closing
from types import SimpleNamespace

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProvisionRequest
from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.debian import DebianRelease
from agentworks.errors import StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.operations import OperationOwner
from agentworks.plugins.azure._creation_request import AzureCreationRequests
from agentworks.plugins.azure._native_route_evidence import RuleAcknowledgment
from agentworks.plugins.azure._owned_auth import _OwnedAzureCredential
from agentworks.plugins.azure.platform import AzureVMPlatform
from tests.plugins.azure.test_activation_sdk import deny
from tests.ssh_fixtures import TEST_SSH_PUBLIC_KEY

SUB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
VM_ID = "11234567-89ab-0cde-0123-456789abcdef"


@pytest.fixture
def owned(tmp_path, monkeypatch):
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
        debian_release=DebianRelease.BOOKWORM,
        hostname="project-vm",
        system_slug="project",
        instance_marker="a" * 32,
        admin_username="operator",
        ssh_public_key=TEST_SSH_PUBLIC_KEY,
        ssh_private_key=None,
        tailscale_auth_key="join-secret-sentinel",
        progress=SimpleNamespace(),
        cpus=2,
        memory_gib=4,
        disk_gib=40,
        swap_gib=0,
    )
    config = SimpleNamespace(operator=SimpleNamespace(ssh=SSHSettings(tmp_path / "trust", tmp_path / "identity")))
    context = RunContext(config=config)
    with closing(Database(tmp_path / "factory.db")) as database:
        owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "vm"), "proof")
        access = platform.build_creation_access(request, context, owner=owner, custody=LocalDeliveryCustody())
        yield SimpleNamespace(
            access=access,
            owner=owner,
            platform=platform,
            request=request,
            context=context,
            database=database,
        )


def test_factory_is_passive_and_public_configuration_has_no_join_secret(owned):
    access = owned.access
    assert access.binding is None and dict(access.platform_metadata) == {}
    assert owned.owner.list_pending_lifecycle_obligations() == ()
    vm = access._requests.plan[-1]
    startup = base64.b64decode(vm.body["properties"]["osProfile"]["customData"])
    assert owned.request.tailscale_auth_key.encode() not in startup
    assert not access._auth.has_credential
    assert access.settle(Deadline.after(2))


def test_factory_refuses_foreign_owner_before_effects(owned):
    owned.request.vm_name = "other"
    with pytest.raises(ValidationError):
        owned.platform.build_creation_access(
            owned.request,
            owned.context,
            owner=owned.owner,
            custody=LocalDeliveryCustody(),
        )


def test_partial_receipts_publish_addressability_without_unobserved_vm_id(owned):
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


def test_acceptance_never_reopens_rollback_even_after_publication_failure(owned, monkeypatch):
    access = owned.access
    access._result = object()
    monkeypatch.setattr(access._requests, "reconcile", lambda deadline: (_ for _ in ()).throw(OSError("lost reply")))
    with pytest.raises(OSError):
        access.complete_bootstrap(Deadline.after(2))
    with pytest.raises(StateError):
        access.request_rollback()


def test_cleanup_budget_cannot_extend_original_provisioning_deadline(owned):
    access = owned.access
    original = Deadline.after(1)
    access._deadline = original
    assert access._budget(Deadline.after(60)).expires_at == original.expires_at


def test_public_route_readiness_trust_and_probe_precede_binding_and_transfer(owned, monkeypatch):
    from agentworks.plugins.azure import _creation_access as module
    from agentworks.vms import _creation_ssh

    access = owned.access
    requests = access._requests
    models, events = {}, []
    credential = object()
    deadline = Deadline.after(10)
    monkeypatch.setattr(_OwnedAzureCredential, "credential", lambda self, budget: credential)

    def create(index, selected, budget):
        assert selected is credential and budget is deadline
        assert owned.owner.list_pending_lifecycle_obligations()
        if not requests._marked:
            requests._marked = True
            row = owned.owner.inspect_lifecycle_obligation(requests._id)
            from agentworks.operations import LifecycleObligation

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
        def __init__(self, *args):
            self.cleanup_incomplete = False

        def open(self, budget):
            assert access._route is self and budget is deadline
            events.append("route")

        def check(self, budget, *, observed_vm):
            assert observed_vm.vm_id == VM_ID and budget is deadline

        def close(self, budget):
            return True

        def settle(self, budget):
            return True

    class Ready:
        def wait(self, endpoint, budget):
            assert access._readiness is self and endpoint == "192.0.2.10" and budget is deadline
            events.append("listening")

        def close(self):
            return True

    class Trust:
        def __init__(self, owner, **kwargs):
            assert owner is owned.owner and kwargs["instance_marker"] == owned.request.instance_marker

        def prepare(self):
            assert access._trust is self
            events.append("trust")

        def close(self, budget):
            return True

    def probe(connection, marker, budget, custody, *, bootstrap_check):
        assert connection.host == "192.0.2.10" and marker == owned.request.instance_marker
        assert budget is deadline and custody is access._custody and bootstrap_check
        events.append("probe")

    monkeypatch.setattr(module, "AzureNativeRoute", Route)
    monkeypatch.setattr(module, "CreationSSHTrust", Trust)
    monkeypatch.setattr(_creation_ssh, "SSHReadiness", Ready)
    monkeypatch.setattr(_creation_ssh, "observe_early_guest", probe)
    owned.context.config.operator.ssh_allow_cidrs = []
    result = access.provision(deadline)
    assert events == ["route", "listening", "trust", "probe"]
    assert result.binding is access.binding and result.binding.delivery_account == "operator"
    assert result.platform_metadata["vm_id"] == VM_ID
    assert result.locator.token == f"azure-vm:v2:{VM_ID}:{requests.resources[-1].resource_id}"
    access.complete_bootstrap(deadline)
    with pytest.raises(StateError):
        access.request_rollback()
    assert access.settle(deadline)
    assert owned.owner.list_pending_lifecycle_obligations() == ()
