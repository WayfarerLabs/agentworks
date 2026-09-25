"""Provider-locator observations for the three cloud VM platforms."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform.base import ProviderLocator
from agentworks.errors import AlreadyExistsError, ConfigError, LimitExceededError, NotFoundError, StateError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.aws.network import EC2Error
from agentworks.plugins.aws.platform import EC2Platform
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.plugins.gcp.platform import GCEPlatform


def _aws_vm(**metadata: str) -> Any:
    return SimpleNamespace(
        name="aws-vm",
        platform_metadata={
            "instance_id": "i-0123456789abcdef0",
            "region": "us-east-1",
            "account_id": "111122223333",
            **metadata,
        },
    )


class _EC2:
    def __init__(
        self,
        response: object | None = None,
        failure: Exception | None = None,
        close_failure: Exception | None = None,
    ) -> None:
        self.response = response or {
            "Reservations": [
                {
                    "OwnerId": "111122223333",
                    "Instances": [
                        {
                            "InstanceId": "i-0123456789abcdef0",
                            "State": {"Name": "running"},
                        }
                    ],
                }
            ]
        }
        self.failure = failure
        self.close_failure = close_failure
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def describe_instances(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.response

    def close(self) -> None:
        self.closed = True
        if self.close_failure is not None:
            raise self.close_failure


def test_aws_locator_reads_one_fresh_exact_instance_with_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = EC2Platform("aws", {"region": "us-east-1", "auth": {"mode": "ambient"}})
    ec2 = _EC2()
    client_calls: list[dict[str, object]] = []

    def client(_service: str, **kwargs: object) -> _EC2:
        client_calls.append(kwargs)
        return ec2

    monkeypatch.setattr(platform, "_get_session", lambda _ctx: SimpleNamespace(client=client))

    observed = platform.observe_provider_locator(_aws_vm(), RunContext(), deadline=Deadline.after(10))

    assert observed == ProviderLocator("aws-ec2:111122223333:us-east-1:i-0123456789abcdef0")
    assert ec2.calls == [{"InstanceIds": ["i-0123456789abcdef0"]}]
    assert ec2.closed
    config = cast("Any", client_calls[0]["config"])
    assert config.connect_timeout is not None
    assert config.read_timeout is not None
    assert config.retries["total_max_attempts"] == 1


def test_aws_locator_rejects_absence_and_wrong_account(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = EC2Platform("aws", {"region": "us-east-1", "auth": {"mode": "ambient"}})
    absent = _EC2(response={"Reservations": []})
    monkeypatch.setattr(platform, "_get_session", lambda _ctx: SimpleNamespace(client=lambda *_a, **_k: absent))
    with pytest.raises(NotFoundError):
        platform.observe_provider_locator(_aws_vm(), RunContext(), deadline=Deadline.after(10))

    wrong_account = _EC2(
        response={
            "Reservations": [
                {
                    "OwnerId": "999988887777",
                    "Instances": [{"InstanceId": "i-0123456789abcdef0", "State": {"Name": "running"}}],
                }
            ]
        }
    )
    monkeypatch.setattr(
        platform,
        "_get_session",
        lambda _ctx: SimpleNamespace(client=lambda *_a, **_k: wrong_account),
    )
    with pytest.raises(StateError):
        platform.observe_provider_locator(_aws_vm(), RunContext(), deadline=Deadline.after(10))


def test_aws_locator_rejects_expired_observation_before_provider_read(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = EC2Platform("aws", {"region": "us-east-1", "auth": {"mode": "ambient"}})
    called = False

    def client(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("provider client must not be built after deadline expiry")

    monkeypatch.setattr(platform, "_get_session", lambda _ctx: SimpleNamespace(client=client))
    with pytest.raises(LimitExceededError):
        platform.observe_provider_locator(_aws_vm(), RunContext(), deadline=Deadline.after(0))
    assert not called


def test_aws_locator_rejects_malformed_metadata_before_session_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = EC2Platform("aws", {"region": "us-east-1", "auth": {"mode": "ambient"}})
    called = False

    def session(_ctx: RunContext) -> object:
        nonlocal called
        called = True
        raise AssertionError("session must not be built for malformed metadata")

    monkeypatch.setattr(platform, "_get_session", session)

    with pytest.raises(StateError):
        platform.observe_provider_locator(_aws_vm(instance_id="bad"), RunContext(), deadline=Deadline.after(10))
    assert not called


def test_aws_locator_preserves_domain_error_from_session_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = EC2Platform("aws", {"region": "us-east-1", "auth": {"mode": "ambient"}})
    failure = ConfigError(
        "resolved AWS credential is unavailable",
        entity_kind="vm-site",
        entity_name="aws",
        hint="resolve the configured credential before retrying",
    )
    monkeypatch.setattr(platform, "_get_session", lambda _ctx: (_ for _ in ()).throw(failure))

    with pytest.raises(ConfigError) as exc_info:
        platform.observe_provider_locator(_aws_vm(), RunContext(), deadline=Deadline.after(10))

    assert exc_info.value is failure
    assert exc_info.value.entity_kind == "vm-site"
    assert exc_info.value.entity_name == "aws"
    assert exc_info.value.hint == "resolve the configured credential before retrying"


def test_aws_locator_close_does_not_mask_describe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = EC2Platform("aws", {"region": "us-east-1", "auth": {"mode": "ambient"}})
    ec2 = _EC2(failure=RuntimeError("describe failed"), close_failure=RuntimeError("close failed"))
    monkeypatch.setattr(platform, "_get_session", lambda _ctx: SimpleNamespace(client=lambda *_a, **_k: ec2))

    with pytest.raises(EC2Error, match="describe failed"):
        platform.observe_provider_locator(_aws_vm(), RunContext(), deadline=Deadline.after(10))

    assert ec2.closed


def test_aws_locator_rejects_a_late_provider_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.plugins.aws import platform as aws_platform

    platform = EC2Platform("aws", {"region": "us-east-1", "auth": {"mode": "ambient"}})
    ec2 = _EC2()
    monkeypatch.setattr(platform, "_get_session", lambda _ctx: SimpleNamespace(client=lambda *_a, **_k: ec2))
    calls = 0

    def remaining(_deadline: Deadline, *, vm_name: str) -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LimitExceededError("expired", entity_kind="vm", entity_name=vm_name)
        return 1

    monkeypatch.setattr(aws_platform, "provider_locator_remaining", remaining)

    with pytest.raises(LimitExceededError):
        platform.observe_provider_locator(_aws_vm(), RunContext(), deadline=Deadline.after(10))
    assert len(ec2.calls) == 1
    assert ec2.closed


_AZURE_RESOURCE_ID = "/subscriptions/sub-A/resourceGroups/rg1/providers/Microsoft.Compute/virtualMachines/vm1"


def _azure_vm(resource_id: str = _AZURE_RESOURCE_ID) -> Any:
    return SimpleNamespace(name="azure-vm", platform_metadata={"resource_id": resource_id})


class _AzureVMs:
    def __init__(self, result: object | None = None, failure: Exception | None = None) -> None:
        self.result = result if result is not None else SimpleNamespace(id=_AZURE_RESOURCE_ID)
        self.failure = failure
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def get(self, resource_group: str, name: str, **kwargs: object) -> object:
        self.calls.append((resource_group, name, kwargs))
        if self.failure is not None:
            raise self.failure
        return self.result


def test_azure_locator_reads_exact_arm_id_with_all_retries_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = AzureVMPlatform(
        "azure",
        {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus", "auth": {"mode": "ambient"}},
    )
    vms = _AzureVMs()
    monkeypatch.setattr(platform, "_compute_client", lambda _az, _ctx: SimpleNamespace(virtual_machines=vms))

    observed = platform.observe_provider_locator(_azure_vm(), RunContext(), deadline=Deadline.after(10))

    assert observed == ProviderLocator(f"azure-vm:{_AZURE_RESOURCE_ID}")
    resource_group, name, kwargs = vms.calls[0]
    assert (resource_group, name) == ("rg1", "vm1")
    assert kwargs["retry_total"] == kwargs["retry_connect"] == kwargs["retry_read"] == kwargs["retry_status"] == 0
    assert kwargs["timeout"] is not None
    assert kwargs["connection_timeout"] is not None
    assert kwargs["read_timeout"] is not None


def test_azure_locator_accepts_unicode_resource_group_name(monkeypatch: pytest.MonkeyPatch) -> None:
    resource_id = "/subscriptions/sub-A/resourceGroups/région/providers/Microsoft.Compute/virtualMachines/vm1"
    platform = AzureVMPlatform(
        "azure",
        {"subscription_id": "sub-A", "resource_group": "région", "region": "eastus", "auth": {"mode": "ambient"}},
    )
    vms = _AzureVMs(result=SimpleNamespace(id=resource_id))
    monkeypatch.setattr(platform, "_compute_client", lambda _az, _ctx: SimpleNamespace(virtual_machines=vms))

    observed = platform.observe_provider_locator(_azure_vm(resource_id), RunContext(), deadline=Deadline.after(10))

    assert observed == ProviderLocator(f"azure-vm:{resource_id}")
    assert (vms.calls[0][0], vms.calls[0][1]) == ("région", "vm1")


def test_azure_locator_rejects_malformed_or_changed_arm_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = AzureVMPlatform(
        "azure",
        {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus", "auth": {"mode": "ambient"}},
    )
    with pytest.raises(StateError):
        platform.observe_provider_locator(_azure_vm("not-an-arm-id"), RunContext(), deadline=Deadline.after(10))

    vms = _AzureVMs(result=SimpleNamespace(id=_AZURE_RESOURCE_ID + "-other"))
    monkeypatch.setattr(platform, "_compute_client", lambda _az, _ctx: SimpleNamespace(virtual_machines=vms))
    with pytest.raises(StateError):
        platform.observe_provider_locator(_azure_vm(), RunContext(), deadline=Deadline.after(10))


def test_azure_locator_maps_provider_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from azure.core.exceptions import ResourceNotFoundError

    platform = AzureVMPlatform(
        "azure",
        {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus", "auth": {"mode": "ambient"}},
    )
    vms = _AzureVMs(failure=ResourceNotFoundError(message="gone"))
    monkeypatch.setattr(platform, "_compute_client", lambda _az, _ctx: SimpleNamespace(virtual_machines=vms))

    with pytest.raises(NotFoundError):
        platform.observe_provider_locator(_azure_vm(), RunContext(), deadline=Deadline.after(10))


def test_azure_locator_rejects_expired_observation_before_compute_client(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = AzureVMPlatform(
        "azure",
        {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus", "auth": {"mode": "ambient"}},
    )
    called = False

    def compute_client(*_args: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("compute client must not be built after deadline expiry")

    monkeypatch.setattr(platform, "_compute_client", compute_client)
    with pytest.raises(LimitExceededError):
        platform.observe_provider_locator(_azure_vm(), RunContext(), deadline=Deadline.after(0))
    assert not called


def test_azure_locator_rejects_a_late_provider_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.plugins.azure import platform as azure_platform

    platform = AzureVMPlatform(
        "azure",
        {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus", "auth": {"mode": "ambient"}},
    )
    vms = _AzureVMs()
    monkeypatch.setattr(platform, "_compute_client", lambda _az, _ctx: SimpleNamespace(virtual_machines=vms))
    calls = 0

    def remaining(_deadline: Deadline, *, vm_name: str) -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LimitExceededError("expired", entity_kind="vm", entity_name=vm_name)
        return 1

    monkeypatch.setattr(azure_platform, "provider_locator_remaining", remaining)

    with pytest.raises(LimitExceededError):
        platform.observe_provider_locator(_azure_vm(), RunContext(), deadline=Deadline.after(10))
    assert len(vms.calls) == 1


_GCP_METADATA = {
    "project_id": "project-a",
    "zone": "us-central1-a",
    "instance_name": "vm-a",
    "instance_id": "201",
    "network_url": "projects/project-a/global/networks/default",
    "network_tag": "tag-a",
    "allow_rule": "allow-a",
    "allow_rule_id": "202",
    "allow_source_ranges": "198.51.100.1/32",
    "deny_rule": "deny-a",
    "deny_rule_id": "203",
    "access_config_name": "External NAT",
}


def _gcp_vm(**metadata: str) -> Any:
    return SimpleNamespace(name="gcp-vm", platform_metadata={**_GCP_METADATA, **metadata})


class _GCPInstances:
    def __init__(self, result: object | None = None, failure: Exception | None = None) -> None:
        self.result = result if result is not None else SimpleNamespace(id=201)
        self.failure = failure
        self.calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.result


def test_gcp_locator_reads_owned_incarnation_with_remaining_timeout() -> None:
    platform = GCEPlatform(
        "gcp",
        {"project_id": "project-a", "zone": "us-central1-a", "auth": {"mode": "ambient"}},
    )
    instances = _GCPInstances()
    platform._clients = cast(
        "Any", SimpleNamespace(client=lambda service, _ctx: instances if service == "instances" else None)
    )

    observed = platform.observe_provider_locator(_gcp_vm(), RunContext(), deadline=Deadline.after(10))

    assert observed == ProviderLocator("gcp-gce:project-a:us-central1-a:201")
    assert instances.calls[0]["retry"] is None
    assert instances.calls[0]["timeout"] is not None


def test_gcp_locator_ignores_unrelated_metadata() -> None:
    platform = GCEPlatform(
        "gcp",
        {"project_id": "project-a", "zone": "us-central1-a", "auth": {"mode": "ambient"}},
    )
    instances = _GCPInstances()
    platform._clients = cast(
        "Any", SimpleNamespace(client=lambda service, _ctx: instances if service == "instances" else None)
    )
    vm = SimpleNamespace(
        name="gcp-vm",
        platform_metadata={
            "project_id": _GCP_METADATA["project_id"],
            "zone": _GCP_METADATA["zone"],
            "instance_name": _GCP_METADATA["instance_name"],
            "instance_id": _GCP_METADATA["instance_id"],
            "network_url": "not-a-network-url",
            "allow_source_ranges": "not-a-cidr",
        },
    )

    observed = platform.observe_provider_locator(vm, RunContext(), deadline=Deadline.after(10))

    assert observed == ProviderLocator("gcp-gce:project-a:us-central1-a:201")


@pytest.mark.parametrize(
    "metadata",
    [
        {"project_id": "", "zone": "us-central1-a", "instance_name": "vm-a", "instance_id": "201"},
        {"project_id": "project-a", "zone": "us-central1-a", "instance_name": "vm-a"},
        {"project_id": "project-a", "zone": "us-central1-a", "instance_name": "vm/a", "instance_id": "201"},
        {"project_id": "project-a", "zone": "us-central1-a", "instance_name": "vm-a", "instance_id": "bad"},
    ],
)
def test_gcp_locator_rejects_malformed_identity_before_provider_read(metadata: dict[str, str]) -> None:
    platform = GCEPlatform(
        "gcp",
        {"project_id": "project-a", "zone": "us-central1-a", "auth": {"mode": "ambient"}},
    )
    called = False

    def client(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("provider client must not be built for malformed metadata")

    platform._clients = cast("Any", SimpleNamespace(client=client))

    with pytest.raises(StateError):
        platform.observe_provider_locator(
            SimpleNamespace(name="gcp-vm", platform_metadata=metadata),
            RunContext(),
            deadline=Deadline.after(10),
        )
    assert not called


def test_gcp_locator_rejects_changed_incarnation() -> None:
    platform = GCEPlatform(
        "gcp",
        {"project_id": "project-a", "zone": "us-central1-a", "auth": {"mode": "ambient"}},
    )
    instances = _GCPInstances(result=SimpleNamespace(id=999))
    platform._clients = cast(
        "Any", SimpleNamespace(client=lambda service, _ctx: instances if service == "instances" else None)
    )

    with pytest.raises(AlreadyExistsError):
        platform.observe_provider_locator(_gcp_vm(), RunContext(), deadline=Deadline.after(10))


def test_gcp_locator_maps_provider_not_found() -> None:
    from google.api_core import exceptions as api_exceptions

    platform = GCEPlatform(
        "gcp",
        {"project_id": "project-a", "zone": "us-central1-a", "auth": {"mode": "ambient"}},
    )
    instances = _GCPInstances(failure=cast("Any", api_exceptions.NotFound)("gone"))
    platform._clients = cast(
        "Any", SimpleNamespace(client=lambda service, _ctx: instances if service == "instances" else None)
    )

    with pytest.raises(NotFoundError):
        platform.observe_provider_locator(_gcp_vm(), RunContext(), deadline=Deadline.after(10))


def test_gcp_locator_rejects_a_late_provider_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.plugins.gcp import platform as gcp_platform

    platform = GCEPlatform(
        "gcp",
        {"project_id": "project-a", "zone": "us-central1-a", "auth": {"mode": "ambient"}},
    )
    instances = _GCPInstances()
    platform._clients = cast(
        "Any", SimpleNamespace(client=lambda service, _ctx: instances if service == "instances" else None)
    )
    calls = 0

    def remaining(_deadline: Deadline, *, vm_name: str) -> float:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise LimitExceededError("expired", entity_kind="vm", entity_name=vm_name)
        return 1

    monkeypatch.setattr(gcp_platform, "provider_locator_remaining", remaining)

    with pytest.raises(LimitExceededError):
        platform.observe_provider_locator(_gcp_vm(), RunContext(), deadline=Deadline.after(10))
    assert len(instances.calls) == 1
