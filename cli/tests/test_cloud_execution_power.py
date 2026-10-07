"""Offline exact-instance, passive power observations for cloud platforms."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.compute.models import InstanceViewStatus, VirtualMachine, VirtualMachineInstanceView
from botocore.exceptions import ClientError
from google.api_core.exceptions import NotFound
from google.cloud.compute_v1 import Instance

from agentworks.capabilities.base import RunContext
from agentworks.db import VMStatus
from agentworks.errors import AlreadyExistsError, LimitExceededError, NotFoundError, StateError, ValidationError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.plugins.aws.network import EC2Error
from agentworks.plugins.aws.platform import EC2Platform
from agentworks.plugins.azure.platform import AzureVMPlatform
from agentworks.plugins.gcp.platform import GCEPlatform

_ARM_ID = "/subscriptions/sub-a/resourceGroups/rg-a/providers/Microsoft.Compute/virtualMachines/vm-a"
_INSTANCE_ID = "i-0123456789abcdef0"
_ACCOUNT = "111122223333"


def _aws_response(state: object = "running") -> dict[str, Any]:
    return {
        "Reservations": [{"OwnerId": _ACCOUNT, "Instances": [{"InstanceId": _INSTANCE_ID, "State": {"Name": state}}]}]
    }


def _azure_response(codes: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        id=_ARM_ID, instance_view=SimpleNamespace(statuses=[SimpleNamespace(code=code) for code in codes])
    )


@pytest.fixture(params=["aws", "azure", "gcp"])
def observer(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Only fixed, offline read methods exist on the provider fixtures."""
    kind = request.param
    read = Mock()
    build = Mock()
    if kind == "aws":
        platform: Any = EC2Platform("power-aws", {"region": "us-east-1", "auth": {"mode": "ambient"}})
        metadata = {"instance_id": _INSTANCE_ID, "region": "us-east-1", "account_id": _ACCOUNT}
        read.return_value = _aws_response()
        close = Mock()
        build.return_value = SimpleNamespace(describe_instances=read, close=close)
        monkeypatch.setattr(platform, "_get_session", lambda _ctx: SimpleNamespace(client=build))
    elif kind == "azure":
        platform = AzureVMPlatform(
            "power-azure",
            {"subscription_id": "sub-a", "resource_group": "rg-a", "region": "eastus", "auth": {"mode": "ambient"}},
        )
        metadata = {"resource_id": _ARM_ID}
        read.return_value = VirtualMachine(
            {
                "id": _ARM_ID,
                "properties": {
                    "instanceView": VirtualMachineInstanceView(statuses=[InstanceViewStatus(code="PowerState/running")])
                },
            }
        )
        build.return_value = SimpleNamespace(virtual_machines=SimpleNamespace(get=read))
        monkeypatch.setattr(platform, "_compute_client", build)
        close = None
    else:
        platform = GCEPlatform("power-gcp", {"project_id": "project-a", "zone": "us-central1-a"})
        metadata = {"project_id": "project-a", "zone": "us-central1-a", "instance_name": "vm-a", "instance_id": "201"}
        read.return_value = Instance(id=201, status="RUNNING")
        build.return_value = SimpleNamespace(get=read)
        monkeypatch.setattr(platform._clients, "client", build)
        close = None
    return SimpleNamespace(
        kind=kind,
        platform=platform,
        vm=SimpleNamespace(name=f"cloud-power-{kind}", platform_metadata=metadata),
        read=read,
        build=build,
        close=close,
    )


def _observe(observer: SimpleNamespace, deadline: Deadline | None = None) -> VMStatus:
    return cast(
        VMStatus,
        observer.platform.observe_execution_power(
            observer.vm,
            RunContext(),
            deadline=deadline if deadline is not None else Deadline.after(10),
            custody=LocalDeliveryCustody(),
        ),
    )


def _positive_budget(value: object) -> None:
    assert isinstance(value, float) and math.isfinite(value) and 0 < value <= 10


def test_power_uses_one_exact_read_with_finite_sdk_timeouts_and_no_retry(observer: SimpleNamespace) -> None:
    assert _observe(observer) is VMStatus.RUNNING
    observer.read.assert_called_once()
    kwargs = observer.read.call_args.kwargs
    if observer.kind == "aws":
        assert kwargs == {"InstanceIds": [_INSTANCE_ID]}
        args, client_kwargs = observer.build.call_args
        assert args == ("ec2",)
        assert client_kwargs["region_name"] == "us-east-1"
        config = client_kwargs["config"]
        _positive_budget(config.connect_timeout)
        _positive_budget(config.read_timeout)
        assert config.retries == {"total_max_attempts": 1, "mode": "standard"}
        observer.close.assert_called_once_with()
    elif observer.kind == "azure":
        assert observer.read.call_args.args == ("rg-a", "vm-a")
        assert observer.build.call_args.args[0].subscription_id == "sub-a"
        assert kwargs["expand"] == "instanceView"
        for key in ("timeout", "connection_timeout", "read_timeout"):
            _positive_budget(kwargs[key])
        for key in ("retry_total", "retry_connect", "retry_read", "retry_status"):
            assert kwargs[key] == 0
    else:
        assert observer.build.call_args.args[0] == "instances"
        assert kwargs["project"] == "project-a"
        assert kwargs["zone"] == "us-central1-a"
        assert kwargs["instance"] == "vm-a"
        assert kwargs["retry"] is None
        _positive_budget(kwargs["timeout"])


@pytest.mark.parametrize("deadline,error", [(Deadline(None), ValidationError), (Deadline(0), LimitExceededError)])
def test_power_rejects_unbounded_or_expired_budget_before_client(
    observer: SimpleNamespace, deadline: Deadline, error: type[Exception]
) -> None:
    with pytest.raises(error):
        _observe(observer, deadline)
    observer.build.assert_not_called()
    observer.read.assert_not_called()


def test_power_rejects_bad_persisted_identity_before_client(observer: SimpleNamespace) -> None:
    observer.vm.platform_metadata = {}
    with pytest.raises(StateError):
        _observe(observer)
    observer.build.assert_not_called()
    observer.read.assert_not_called()


def test_power_rejects_changed_instance_identity(observer: SimpleNamespace) -> None:
    expected: type[Exception]
    if observer.kind == "aws":
        observer.read.return_value["Reservations"][0]["Instances"][0]["InstanceId"] = "i-11111111111111111"
        expected = EC2Error
    elif observer.kind == "azure":
        observer.read.return_value.id = _ARM_ID + "-replacement"
        expected = StateError
    else:
        observer.read.return_value.id = 999
        expected = AlreadyExistsError
    with pytest.raises(expected):
        _observe(observer)
    observer.read.assert_called_once()


def test_power_rejects_provider_absence(observer: SimpleNamespace) -> None:
    failures = {
        "aws": ClientError({"Error": {"Code": "InvalidInstanceID.NotFound"}}, "DescribeInstances"),
        "azure": ResourceNotFoundError(message="absent"),
        "gcp": cast(Any, NotFound)("absent"),
    }
    observer.read.side_effect = failures[observer.kind]
    with pytest.raises(NotFoundError):
        _observe(observer)
    observer.read.assert_called_once()


def test_power_rejects_malformed_provider_identity(observer: SimpleNamespace) -> None:
    expected: type[Exception]
    if observer.kind == "aws":
        observer.read.return_value = {"Reservations": [{"OwnerId": _ACCOUNT, "Instances": []}]}
        expected = EC2Error
    elif observer.kind == "azure":
        observer.read.return_value = _azure_response(["PowerState/running"])
        observer.read.return_value.id = None
        expected = StateError
    else:
        observer.read.return_value = SimpleNamespace(id=0, status="RUNNING")
        expected = AlreadyExistsError
    with pytest.raises(expected):
        _observe(observer)


def test_power_rejects_late_provider_result(observer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now)
    result = observer.read.return_value

    def late_read(*_args: object, **_kwargs: object) -> object:
        nonlocal now
        now = 111.0
        return result

    observer.read.side_effect = late_read
    with pytest.raises(LimitExceededError):
        _observe(observer, Deadline.after(10))
    observer.read.assert_called_once()
    if observer.close is not None:
        observer.close.assert_called_once()


@pytest.mark.parametrize(
    "state,expected",
    [
        ("running", VMStatus.RUNNING),
        ("stopped", VMStatus.STOPPED),
        ("pending", VMStatus.UNKNOWN),
        ("stopping", VMStatus.UNKNOWN),
        ("shutting-down", VMStatus.UNKNOWN),
        ("terminated", VMStatus.UNKNOWN),
        ("RUNNING", VMStatus.UNKNOWN),
        ("", VMStatus.UNKNOWN),
        (None, VMStatus.UNKNOWN),
        ([], VMStatus.UNKNOWN),
        ({}, VMStatus.UNKNOWN),
    ],
)
@pytest.mark.parametrize("observer", ["aws"], indirect=True)
def test_aws_stable_power_only(observer: SimpleNamespace, state: object, expected: VMStatus) -> None:
    observer.read.return_value = _aws_response(state)
    assert _observe(observer) is expected


@pytest.mark.parametrize("state", [None, [], {}, "running"])
@pytest.mark.parametrize("observer", ["aws"], indirect=True)
def test_aws_malformed_state_container_is_unknown(observer: SimpleNamespace, state: object) -> None:
    observer.read.return_value["Reservations"][0]["Instances"][0]["State"] = state
    assert _observe(observer) is VMStatus.UNKNOWN


@pytest.mark.parametrize(
    "codes,expected",
    [
        (["PowerState/running"], VMStatus.RUNNING),
        (["PowerState/stopped"], VMStatus.STOPPED),
        (["PowerState/deallocated"], VMStatus.DEALLOCATED),
        (["ProvisioningState/succeeded", "PowerState/running"], VMStatus.RUNNING),
    ]
    + [
        (codes, VMStatus.UNKNOWN)
        for codes in (
            [],
            [None],
            [123],
            ["PowerState/starting"],
            ["PowerState/stopping"],
            ["PowerState/deallocating"],
            ["PowerState/unknown"],
            ["PowerState/running", "PowerState/stopped"],
            ["PowerState/running", "PowerState/running"],
            ["ProvisioningState/succeeded"],
        )
    ],
)
@pytest.mark.parametrize("observer", ["azure"], indirect=True)
def test_azure_requires_one_stable_power_status(
    observer: SimpleNamespace, codes: list[object], expected: VMStatus
) -> None:
    observer.read.return_value = _azure_response(codes)
    assert _observe(observer) is expected


@pytest.mark.parametrize("view", [None, SimpleNamespace(statuses=None), SimpleNamespace(statuses="PowerState/running")])
@pytest.mark.parametrize("observer", ["azure"], indirect=True)
def test_azure_missing_or_malformed_view_is_unknown(observer: SimpleNamespace, view: object) -> None:
    observer.read.return_value = SimpleNamespace(id=_ARM_ID, instance_view=view)
    assert _observe(observer) is VMStatus.UNKNOWN


@pytest.mark.parametrize(
    "state,expected",
    [
        ("RUNNING", VMStatus.RUNNING),
        ("TERMINATED", VMStatus.STOPPED),
        ("STOPPED", VMStatus.STOPPED),
        ("PROVISIONING", VMStatus.UNKNOWN),
        ("STAGING", VMStatus.UNKNOWN),
        ("STOPPING", VMStatus.UNKNOWN),
        ("SUSPENDING", VMStatus.UNKNOWN),
        ("SUSPENDED", VMStatus.UNKNOWN),
        ("REPAIRING", VMStatus.UNKNOWN),
        ("PENDING", VMStatus.UNKNOWN),
        ("DEPROVISIONING", VMStatus.UNKNOWN),
        ("running", VMStatus.UNKNOWN),
        ("", VMStatus.UNKNOWN),
        (None, VMStatus.UNKNOWN),
        (1, VMStatus.UNKNOWN),
        ([], VMStatus.UNKNOWN),
        ({}, VMStatus.UNKNOWN),
    ],
)
@pytest.mark.parametrize("observer", ["gcp"], indirect=True)
def test_gcp_stable_power_only(observer: SimpleNamespace, state: object, expected: VMStatus) -> None:
    observer.read.return_value = SimpleNamespace(id=201, status=state)
    assert _observe(observer) is expected


@pytest.mark.parametrize("observer", ["aws"], indirect=True)
def test_aws_wrong_reservation_owner_refuses_running_power(observer: SimpleNamespace) -> None:
    observer.read.return_value["Reservations"][0]["OwnerId"] = "999988887777"
    with pytest.raises(StateError):
        _observe(observer)


@pytest.mark.parametrize("hook", ["observe_execution_power", "observe_provider_locator"])
def test_expiry_during_client_setup_prevents_dispatch(
    observer: SimpleNamespace, hook: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now)
    built = observer.build.return_value

    def slow_build(*_args: object, **_kwargs: object) -> object:
        nonlocal now
        now = 111.0
        return built

    observer.build.side_effect = slow_build
    with pytest.raises(LimitExceededError):
        getattr(observer.platform, hook)(
            observer.vm, RunContext(), deadline=Deadline.after(10), custody=LocalDeliveryCustody()
        )
    observer.read.assert_not_called()
    if observer.close is not None:
        observer.close.assert_called_once()


@pytest.mark.parametrize("hook", ["observe_execution_power", "observe_provider_locator"])
@pytest.mark.parametrize("observer", ["aws"], indirect=True)
def test_aws_expiry_during_session_setup_prevents_client_build(
    observer: SimpleNamespace, hook: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now)

    def slow_session(_ctx: RunContext) -> object:
        nonlocal now
        now = 111.0
        return SimpleNamespace(client=observer.build)

    monkeypatch.setattr(observer.platform, "_get_session", slow_session)
    with pytest.raises(LimitExceededError):
        getattr(observer.platform, hook)(
            observer.vm, RunContext(), deadline=Deadline.after(10), custody=LocalDeliveryCustody()
        )
    observer.build.assert_not_called()
    observer.read.assert_not_called()


def test_sdk_timeout_uses_budget_remaining_after_setup(
    observer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now)
    built = observer.build.return_value

    def slow_setup(*_args: object, **_kwargs: object) -> object:
        nonlocal now
        now = 104.0
        return built

    if observer.kind == "aws":

        def session(_ctx: RunContext) -> object:
            nonlocal now
            now = 104.0
            return SimpleNamespace(client=observer.build)

        monkeypatch.setattr(observer.platform, "_get_session", session)
    else:
        observer.build.side_effect = slow_setup
    assert _observe(observer, Deadline.after(10)) is VMStatus.RUNNING
    if observer.kind == "aws":
        config = observer.build.call_args.kwargs["config"]
        assert config.connect_timeout == config.read_timeout == 6
    else:
        assert observer.read.call_args.kwargs["timeout"] == 6


def test_actual_sdk_exact_request_and_response_contract(
    observer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise installed SDK serialization with all network delivery replaced."""
    if observer.kind == "aws":
        import boto3
        from botocore.stub import Stubber

        session = boto3.Session(aws_access_key_id="offline-power", aws_secret_access_key="offline-power")
        stubbers = []
        clients = []

        def client(service: str, **kwargs: Any) -> Any:
            ec2 = session.client(service, **kwargs)
            stubber = Stubber(ec2)
            stubber.add_response("describe_instances", _aws_response(), {"InstanceIds": [_INSTANCE_ID]})
            stubber.activate()
            stubbers.append(stubber)
            clients.append(ec2)
            return ec2

        monkeypatch.setattr(observer.platform, "_get_session", lambda _ctx: SimpleNamespace(client=client))
        assert _observe(observer) is VMStatus.RUNNING
        stubbers[0].assert_no_pending_responses()
        config = clients[0].meta.config
        _positive_budget(config.connect_timeout)
        _positive_budget(config.read_timeout)
        assert config.retries["total_max_attempts"] == 1
        return

    requests = pytest.importorskip("requests")
    response = requests.Response()
    response.status_code = 200
    response._content_consumed = True
    response.raw = SimpleNamespace(enforce_content_length=False)
    response.headers["Content-Type"] = "application/json"
    delivered = Mock(return_value=response)
    if observer.kind == "azure":
        from azure.core.credentials import AccessToken
        from azure.core.pipeline.transport import RequestsTransport
        from azure.mgmt.compute import ComputeManagementClient

        response._content = json.dumps(
            {
                "id": _ARM_ID,
                "properties": {"instanceView": {"statuses": [{"code": "PowerState/running"}]}},
            }
        ).encode()
        offline_session = Mock(request=delivered)
        credential = SimpleNamespace(get_token=lambda *_args, **_kwargs: AccessToken("offline-power", 9999999999))
        azure = ComputeManagementClient(
            credential, "sub-a", transport=RequestsTransport(session=offline_session, session_owner=False)
        )
        monkeypatch.setattr(observer.platform, "_compute_client", lambda _config, _ctx: azure)
        try:
            assert _observe(observer) is VMStatus.RUNNING
        finally:
            azure.close()
        delivered.assert_called_once()
        args = delivered.call_args.args
        kwargs = delivered.call_args.kwargs
        assert args[0] == "GET"
        assert _ARM_ID in args[1]
        assert "$expand=instanceView" in args[1] or "%24expand=instanceView" in args[1]
        assert isinstance(kwargs["timeout"], tuple)
        for timeout in kwargs["timeout"]:
            _positive_budget(timeout)
    else:
        from google.auth.credentials import AnonymousCredentials
        from google.cloud.compute_v1 import InstancesClient

        response._content = json.dumps({"id": "201", "status": "RUNNING", "name": "vm-a"}).encode()
        gcp = InstancesClient(credentials=cast(Any, AnonymousCredentials)(), transport="rest")
        transport = cast(Any, gcp.transport)
        monkeypatch.setattr(transport._session, "request", delivered)
        monkeypatch.setattr(observer.platform._clients, "client", lambda _kind, _ctx: gcp)
        try:
            assert _observe(observer) is VMStatus.RUNNING
        finally:
            transport.close()
        delivered.assert_called_once()
        args = delivered.call_args.args
        kwargs = delivered.call_args.kwargs
        assert args[0] == "GET"
        assert args[1].endswith("/projects/project-a/zones/us-central1-a/instances/vm-a")
        _positive_budget(kwargs["timeout"])
