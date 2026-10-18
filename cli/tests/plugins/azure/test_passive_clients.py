"""Locked ARM SDK transport behavior for passive client pipelines."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient

from agentworks.capabilities.base import RunContext
from agentworks.db import VMStatus
from agentworks.errors import LimitExceededError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh import SSHSettings
from agentworks.plugins.azure import _passive_clients
from agentworks.plugins.azure.network import AzureError, _native_read_options
from agentworks.plugins.azure.platform import AzureVMPlatform

SUBSCRIPTION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
VM_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/group/providers/Microsoft.Compute/virtualMachines/vm"
NIC_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/group/providers/Microsoft.Network/networkInterfaces/nic"
PIP_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/group/providers/Microsoft.Network/publicIPAddresses/ip"


class Response(HttpResponse):
    def __init__(self, request: Any, status: int, payload: dict[str, Any], headers: dict[str, str]) -> None:
        super().__init__(request, None)
        self.status_code = status
        self.headers = {"content-type": "application/json", **headers}
        self.content_type = "application/json"
        self.payload = payload

    def body(self) -> bytes:
        return json.dumps(self.payload).encode()

    def json(self) -> dict[str, Any]:
        return self.payload


class ScriptedTransport(HttpTransport):
    def __init__(self, replies: list[tuple[int, dict[str, Any], dict[str, str]]]) -> None:
        self.replies = iter(replies)
        self.calls: list[tuple[str, str, dict[str, str], dict[str, Any]]] = []
        self.closed = 0

    def __enter__(self) -> ScriptedTransport:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.closed += 1

    def send(self, request: Any, **kwargs: Any) -> Response:
        self.calls.append((request.method, request.url, dict(request.headers), kwargs))
        status, payload, headers = next(self.replies)
        return Response(request, status, payload, headers)


class Credential:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.calls.append((scopes, kwargs))
        return AccessToken(f"token-{len(self.calls)}", 9999999999)


def payload(kind: str) -> dict[str, Any]:
    if kind == "compute":
        return {
            "id": VM_ID,
            "properties": {
                "instanceView": {"statuses": [{"code": "PowerState/running"}]},
                "networkProfile": {"networkInterfaces": [{"id": NIC_ID}]},
            },
        }
    if kind == "network":
        return {"id": NIC_ID, "properties": {"ipConfigurations": [{"properties": {"publicIPAddress": {"id": PIP_ID}}}]}}
    return {"id": PIP_ID, "properties": {"ipAddress": "198.51.100.2"}}


def client(kind: str, credential: Credential, transport: ScriptedTransport, *, default: bool = False) -> Any:
    if kind == "compute":
        factory = ComputeManagementClient if default else _passive_clients.compute_read_client
        return factory(credential, SUBSCRIPTION, transport=transport)
    factory_network = NetworkManagementClient if default else _passive_clients.network_read_client
    return factory_network(credential, SUBSCRIPTION, transport=transport)


def read(sdk: Any, kind: str) -> Any:
    operations, name, expand = (
        (sdk.virtual_machines, "vm", "instanceView")
        if kind == "compute"
        else (sdk.network_interfaces, "nic", None)
        if kind == "network"
        else (sdk.public_ip_addresses, "ip", None)
    )
    return operations.get("group", name, expand=expand, **_native_read_options(10))


def registration_required(kind: str) -> dict[str, Any]:
    provider = "Microsoft.Compute" if kind == "compute" else "Microsoft.Network"
    return {"error": {"code": "MissingSubscriptionRegistration", "message": f"Register namespace '{provider}'"}}


@pytest.mark.parametrize("kind", ["compute", "network"])
def test_default_sdk_reaches_registration_poll_and_read_replay(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control proves the real default policy reaches the forbidden effects."""
    sleeps: list[float] = []
    monkeypatch.setattr("azure.mgmt.core.policies._base.time.sleep", sleeps.append)
    transport = ScriptedTransport(
        [
            (409, registration_required(kind), {}),
            (200, {}, {}),
            (200, {"registrationState": "Registered"}, {}),
            (200, payload(kind), {}),
        ]
    )
    with client(kind, Credential(), transport, default=True) as sdk:
        assert read(sdk, kind).id == payload(kind)["id"]
    assert [call[0] for call in transport.calls] == ["GET", "POST", "GET", "GET"]
    assert "/register?" in transport.calls[1][1]
    assert "api-version=2016-02-01" in transport.calls[2][1]
    assert transport.calls[0][1] == transport.calls[3][1]
    assert sleeps == [10]


@pytest.mark.parametrize("kind", ["compute", "network", "public-ip"])
def test_passive_registration_failure_has_one_get_and_no_poll(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("azure.mgmt.core.policies._base.time.sleep", lambda _: pytest.fail("unexpected polling"))
    transport = ScriptedTransport([(409, registration_required(kind), {})])
    with client(kind, Credential(), transport) as sdk, pytest.raises(HttpResponseError) as failure:
        read(sdk, kind)
    assert failure.value.status_code == 409
    assert [call[0] for call in transport.calls] == ["GET"]
    assert transport.closed == 1


@pytest.mark.parametrize("kind", ["compute", "network", "public-ip"])
def test_passive_decodes_ordinary_models_and_preserves_request_budgets(kind: str) -> None:
    credential = Credential()
    transport = ScriptedTransport([(200, payload(kind), {})])
    with client(kind, credential, transport) as sdk:
        result = read(sdk, kind)
    assert result.id == payload(kind)["id"]
    if kind == "compute":
        assert result.instance_view.statuses[0].code == "PowerState/running"
    elif kind == "network":
        assert result.ip_configurations[0].public_ip_address.id == PIP_ID
    else:
        assert result.properties.ip_address == "198.51.100.2"
    assert credential.calls[0][0] == ("https://management.azure.com/.default",)
    method, url, headers, options = transport.calls[0]
    assert method == "GET" and payload(kind)["id"] in url
    assert headers["Authorization"] == "Bearer token-1"
    assert options["connection_timeout"] == options["read_timeout"] == 10


@pytest.mark.parametrize("kind", ["compute", "network"])
@pytest.mark.parametrize("status", [429, 500, 503])
def test_passive_service_failure_is_not_retried(kind: str, status: int) -> None:
    transport = ScriptedTransport([(status, {"error": {"code": "ServiceFailure"}}, {"Retry-After": "1"})])
    with client(kind, Credential(), transport) as sdk, pytest.raises(HttpResponseError):
        read(sdk, kind)
    assert [call[0] for call in transport.calls] == ["GET"]


@pytest.mark.parametrize("kind", ["compute", "network"])
@pytest.mark.parametrize("failure", [ServiceRequestError, ServiceResponseError])
def test_passive_transport_failure_is_not_retried(
    kind: str, failure: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = ScriptedTransport([])

    def send(request: Any, **kwargs: Any) -> Response:
        transport.calls.append((request.method, request.url, dict(request.headers), kwargs))
        raise failure("offline transport failure")

    monkeypatch.setattr(transport, "send", send)
    with client(kind, Credential(), transport) as sdk, pytest.raises(failure):
        read(sdk, kind)
    assert [call[0] for call in transport.calls] == ["GET"]


@pytest.mark.parametrize("kind", ["compute", "network"])
@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_passive_redirect_is_refused_without_following_new_route(kind: str, status: int) -> None:
    transport = ScriptedTransport([(status, {}, {"Location": "https://different.invalid/new-route"})])
    with client(kind, Credential(), transport) as sdk, pytest.raises(HttpResponseError):
        read(sdk, kind)
    assert len(transport.calls) == 1
    assert payload(kind)["id"] in transport.calls[0][1]


def challenged_replies(kind: str) -> list[tuple[int, dict[str, Any], dict[str, str]]]:
    claims = base64.urlsafe_b64encode(b'{"access_token":{"fixture":true}}').decode().rstrip("=")
    return [
        (401, {}, {"WWW-Authenticate": f'Bearer error="insufficient_claims", claims="{claims}"'}),
        (200, payload(kind), {}),
    ]


@pytest.mark.parametrize("kind", ["compute", "network"])
def test_passive_arm_challenge_retains_read_only_authentication_resend(kind: str) -> None:
    credential = Credential()
    transport = ScriptedTransport(challenged_replies(kind))
    with client(kind, credential, transport) as sdk:
        assert read(sdk, kind).id == payload(kind)["id"]
    assert len(transport.calls) == 2
    assert [call[:2] for call in transport.calls] == [("GET", transport.calls[0][1])] * 2
    assert transport.calls[1][2]["Authorization"] == "Bearer token-2"
    assert json.loads(credential.calls[1][1]["claims"]) == {"access_token": {"fixture": True}}


@pytest.mark.parametrize("kind", ["compute", "network"])
def test_passive_unhandled_authentication_challenge_is_not_replayed(kind: str) -> None:
    credential = Credential()
    transport = ScriptedTransport([(401, {}, {"WWW-Authenticate": 'Bearer error="invalid_token"'})])
    with client(kind, credential, transport) as sdk, pytest.raises(HttpResponseError) as failure:
        read(sdk, kind)
    assert failure.value.status_code == 401
    assert len(transport.calls) == len(credential.calls) == 1


def test_real_challenged_vm_read_rejects_late_power_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now[0])
    transport = ScriptedTransport(challenged_replies("compute"))
    original_send = transport.send

    def send(request: Any, **kwargs: Any) -> Response:
        response = original_send(request, **kwargs)
        if len(transport.calls) == 2:
            now[0] = 111.0
        return response

    monkeypatch.setattr(transport, "send", send)
    factory = _passive_clients.compute_read_client
    monkeypatch.setattr(_passive_clients, "compute_read_client", lambda c, s: factory(c, s, transport=transport))
    platform = AzureVMPlatform(
        "azure", {"subscription_id": SUBSCRIPTION, "resource_group": "group", "region": "eastus"}
    )
    monkeypatch.setattr(platform, "_get_credential", lambda _: Credential())
    vm = SimpleNamespace(name="vm", platform_metadata={"resource_id": VM_ID})
    with pytest.raises(LimitExceededError):
        platform.observe_execution_power(vm, RunContext(), deadline=Deadline.after(10), custody=LocalDeliveryCustody())
    assert len(transport.calls) == 2
    assert transport.closed == 1


@pytest.mark.parametrize("failed", [False, True])
def test_owned_compute_close_cannot_replace_read_result_or_failure(
    failed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = ScriptedTransport(
        [(409, registration_required("compute"), {})] if failed else [(200, payload("compute"), {})]
    )
    closed: list[bool] = []

    def close() -> None:
        closed.append(True)
        raise RuntimeError("close failed")

    monkeypatch.setattr(transport, "close", close)
    factory = _passive_clients.compute_read_client
    monkeypatch.setattr(_passive_clients, "compute_read_client", lambda c, s: factory(c, s, transport=transport))
    platform = AzureVMPlatform(
        "azure", {"subscription_id": SUBSCRIPTION, "resource_group": "group", "region": "eastus"}
    )
    monkeypatch.setattr(platform, "_get_credential", lambda _: Credential())
    vm = SimpleNamespace(name="vm", platform_metadata={"resource_id": VM_ID})
    if failed:
        with pytest.raises(AzureError) as failure:
            platform.observe_execution_power(
                vm, RunContext(), deadline=Deadline.after(10), custody=LocalDeliveryCustody()
            )
        assert isinstance(failure.value.__cause__, HttpResponseError)
        assert failure.value.__cause__.status_code == 409
    else:
        assert (
            platform.observe_execution_power(
                vm, RunContext(), deadline=Deadline.after(10), custody=LocalDeliveryCustody()
            )
            is VMStatus.RUNNING
        )
    assert closed == [True]


@pytest.mark.parametrize("failed_read", [None, "vm", "nic", "pip"])
@pytest.mark.parametrize("close_fails", [False, True])
def test_real_native_binding_uses_owned_clients_without_registration_or_legacy_cache(
    failed_read: str | None, close_fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    compute = ScriptedTransport(
        [(409, registration_required("compute"), {})] if failed_read == "vm" else [(200, payload("compute"), {})]
    )
    network = ScriptedTransport(
        [(409, registration_required("network"), {})]
        if failed_read == "nic"
        else [
            (200, payload("network"), {}),
            (409, registration_required("public-ip"), {}) if failed_read == "pip" else (200, payload("public-ip"), {}),
        ]
    )
    credential = Credential()
    if close_fails:

        def close() -> None:
            network.closed += 1
            raise RuntimeError("network close failed")

        monkeypatch.setattr(network, "close", close)
    platform = AzureVMPlatform(
        "azure", {"subscription_id": SUBSCRIPTION, "resource_group": "group", "region": "eastus"}
    )
    # A populated lifecycle cache must never supply a native read client.
    legacy_compute, legacy_network = object(), object()
    platform._compute_cached[SUBSCRIPTION] = legacy_compute  # type: ignore[assignment]
    platform._network_cached[SUBSCRIPTION] = legacy_network  # type: ignore[assignment]
    monkeypatch.setattr(platform, "_get_credential", lambda _: credential)
    compute_factory = _passive_clients.compute_read_client
    network_factory = _passive_clients.network_read_client
    monkeypatch.setattr(_passive_clients, "compute_read_client", lambda c, s: compute_factory(c, s, transport=compute))
    monkeypatch.setattr(_passive_clients, "network_read_client", lambda c, s: network_factory(c, s, transport=network))
    settings = SSHSettings(Path("/absent/trust"), Path("/absent/key"), "/absent/agent", "ssh", 10, 3)
    vm = SimpleNamespace(name="vm", admin_username="admin", platform_metadata={"resource_id": VM_ID})

    def resolve() -> Any:
        return platform.resolve_native_execution_binding(
            vm,
            RunContext(),
            config=SimpleNamespace(operator=SimpleNamespace(ssh=settings)),
            deadline=Deadline.after(10),
        )

    if failed_read:
        with pytest.raises(AzureError) as failure:
            resolve()
        assert isinstance(failure.value.__cause__, HttpResponseError)
        assert failure.value.__cause__.status_code == 409
    else:
        assert resolve().carrier._connection.host == "198.51.100.2"
    assert [call[0] for call in compute.calls] == ["GET"]
    assert [call[0] for call in network.calls] == (
        [] if failed_read == "vm" else ["GET"] if failed_read == "nic" else ["GET", "GET"]
    )
    assert compute.closed == 1
    assert network.closed == (0 if failed_read == "vm" else 1)
    assert platform._compute_cached[SUBSCRIPTION] is legacy_compute
    assert platform._network_cached[SUBSCRIPTION] is legacy_network


def test_platform_import_keeps_passive_sdk_clients_lazy() -> None:
    source = """
import sys
import agentworks.plugins.azure.platform
assert 'agentworks.plugins.azure._passive_clients' not in sys.modules
assert 'azure.mgmt.compute' not in sys.modules
assert 'azure.mgmt.network' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
