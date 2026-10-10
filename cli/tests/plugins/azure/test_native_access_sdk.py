"""Real model, ARM pipeline and credential public-close boundaries, offline."""

from __future__ import annotations

import base64
import json
import platform as host_platform
import socket
import subprocess
import traceback
import webbrowser
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from azure.core.credentials import AccessToken, AccessTokenInfo
from azure.core.exceptions import ClientAuthenticationError
from azure.core.pipeline.transport import HttpRequest as LegacyHttpRequest
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.core.rest import HttpRequest
from azure.identity import ClientSecretCredential, DefaultAzureCredential, InteractiveBrowserCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.compute.models import (
    InstanceViewStatus,
    VirtualMachine,
    VirtualMachineInstanceView,
    VirtualMachineProperties,
)
from azure.mgmt.network import NetworkManagementClient

from agentworks.db import VMStatus
from agentworks.errors import LimitExceededError, NotFoundError, StateError
from agentworks.execution.carrier import Deadline
from agentworks.plugins.azure import _passive_clients
from agentworks.plugins.azure._native_access import AzureOwnedReadAccess
from agentworks.plugins.azure.config import AzureAmbientAuth, AzureServicePrincipalAuth
from agentworks.plugins.azure.network import AzureError

SUB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
UUID = "00112233-4455-6677-8899-aabbccddeeff"
RESOURCE = f"/subscriptions/{SUB}/resourceGroups/group/providers/Microsoft.Compute/virtualMachines/vm"
NIC = f"/subscriptions/{SUB}/resourceGroups/group/providers/Microsoft.Network/networkInterfaces/nic"
PIP = f"/subscriptions/{SUB}/resourceGroups/group/providers/Microsoft.Network/publicIPAddresses/ip"
Reply = tuple[int, dict[str, Any], dict[str, str]]


def deny(*args, **kwargs):
    raise AssertionError("unexpected native socket, process or browser work")


@pytest.fixture(autouse=True)
def offline_native_guard(monkeypatch):
    monkeypatch.setattr(host_platform, "platform", lambda: "Linux-offline-test")
    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setattr(webbrowser, "open", deny)


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
    def __init__(self, replies: list[Reply]) -> None:
        self.replies = iter(replies)
        self.calls: list[Any] = []
        self.closed = 0
        self.close_error: BaseException | None = None

    def open(self) -> None:
        pass

    def __enter__(self) -> ScriptedTransport:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error

    def send(self, request: HttpRequest | LegacyHttpRequest, **kwargs: Any) -> Response:
        self.calls.append((request.method, request.url, dict(request.headers), kwargs))
        status, payload, headers = next(self.replies)
        return Response(request, status, payload, headers)


class Credential:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.closed = 0

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.calls.append((scopes, kwargs))
        return AccessToken(f"token-{len(self.calls)}", 9999999999)

    def close(self) -> None:
        self.closed += 1


def vm_payload() -> dict[str, Any]:
    return {
        "id": RESOURCE,
        "location": "eastus",
        "properties": {
            "vmId": UUID,
            "instanceView": {"statuses": [{"code": "PowerState/running"}]},
            "networkProfile": {"networkInterfaces": [{"id": NIC}]},
        },
    }


def vm_row() -> SimpleNamespace:
    return SimpleNamespace(name="vm", platform_metadata={"resource_id": RESOURCE, "vm_id": UUID})


def platform(auth: AzureAmbientAuth | AzureServicePrincipalAuth | None = None) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(auth=auth or AzureAmbientAuth(mode="ambient")), site_name="site")


def bind_clients(
    monkeypatch: pytest.MonkeyPatch, compute: ScriptedTransport, network: ScriptedTransport | None = None
) -> list[ComputeManagementClient | NetworkManagementClient]:
    compute_factory, network_factory = _passive_clients.compute_read_client, _passive_clients.network_read_client
    originals: list[ComputeManagementClient | NetworkManagementClient] = []

    def construct_compute(credential: object, subscription: str) -> ComputeManagementClient:
        client = compute_factory(credential, subscription, transport=compute)
        originals.append(client)
        return client

    def construct_network(credential: object, subscription: str) -> NetworkManagementClient:
        assert network is not None
        client = network_factory(credential, subscription, transport=network)
        originals.append(client)
        return client

    monkeypatch.setattr(_passive_clients, "compute_read_client", construct_compute)
    monkeypatch.setattr(_passive_clients, "network_read_client", construct_network)
    return originals


@pytest.mark.parametrize("shape", ["raw", "properties", "flattened"])
def test_real_vm_model_exposes_nested_and_flattened_identity(shape):
    properties = VirtualMachineProperties()
    properties.instance_view = VirtualMachineInstanceView()
    properties.vm_id = UUID
    assert properties.instance_view is not None
    properties.instance_view.statuses = [InstanceViewStatus(code="PowerState/running")]
    if shape == "raw":
        vm = VirtualMachine(vm_payload())
    elif shape == "properties":
        vm = VirtualMachine(location="eastus", properties=properties)
    else:
        options: dict[str, Any] = {"location": "eastus", "vm_id": UUID, "instance_view": properties.instance_view}
        vm = VirtualMachine(**options)
    assert vm.vm_id == UUID and vm.properties is not None and vm.properties.vm_id == UUID
    assert vm.instance_view is not None and vm.instance_view.statuses is not None
    assert vm.instance_view.statuses[0].code == "PowerState/running"


@pytest.mark.parametrize("outcome", ["success", "404", "late"])
def test_real_read_options_absence_and_original_deadline(monkeypatch, outcome):
    now = [100.0]
    monkeypatch.setattr("agentworks.execution.carrier.time.monotonic", lambda: now[0])
    credential = Credential()
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda **kwargs: credential)
    canary = "offline-404-provider-body-canary"
    transport = ScriptedTransport(
        [(404, {"error": {"code": "ResourceNotFound", "message": canary}}, {})]
        if outcome == "404"
        else [(200, vm_payload(), {})]
    )
    if outcome == "late":
        send = transport.send

        def late(*args: Any, **kwargs: Any) -> Response:
            result = send(*args, **kwargs)
            now[0] = 111.0
            return result

        monkeypatch.setattr(transport, "send", late)
    bind_clients(monkeypatch, transport)
    access = AzureOwnedReadAccess(vm_row(), platform(), SimpleNamespace(secret=deny))
    if outcome == "success":
        assert access.observe_power(Deadline.after(10)) is VMStatus.RUNNING
    else:
        with pytest.raises(NotFoundError if outcome == "404" else LimitExceededError) as caught:
            access.observe_power(Deadline.after(10))
        if outcome == "404":
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            assert canary not in "".join(traceback.format_exception(caught.value))
    assert transport.closed == 1 and len(transport.calls) == 1
    method, url, headers, options = transport.calls[0]
    assert method == "GET" and RESOURCE in url
    assert parse_qs(urlsplit(url).query)["$expand"] == ["instanceView"]
    assert 0 < options["connection_timeout"] == options["read_timeout"] <= 10
    assert headers["Authorization"].startswith("Bearer token-")
    assert access.close(Deadline.after(10)) and credential.closed == 1


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_real_read_client_public_close_keeps_originals_and_dependency_order(monkeypatch, failure):
    compute_factory, network_factory = _passive_clients.compute_read_client, _passive_clients.network_read_client
    for endpoint in [False, True]:
        # Each iteration patches the original factories, not the preceding wrapper.
        monkeypatch.setattr(_passive_clients, "compute_read_client", compute_factory)
        monkeypatch.setattr(_passive_clients, "network_read_client", network_factory)
        credential = Credential()
        monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda credential=credential, **kwargs: credential)
        compute = ScriptedTransport([(200, vm_payload(), {})])
        network = ScriptedTransport(
            [
                (
                    200,
                    {"id": NIC, "properties": {"ipConfigurations": [{"properties": {"publicIPAddress": {"id": PIP}}}]}},
                    {},
                ),
                (200, {"id": PIP, "properties": {"ipAddress": "198.51.100.2"}}, {}),
            ]
        )
        retiring = network if endpoint else compute
        retiring.close_error = failure("offline public close failure") if failure else None
        originals = bind_clients(monkeypatch, compute, network)
        access = AzureOwnedReadAccess(vm_row(), platform(), SimpleNamespace(secret=deny))
        observe = access.observe_public_endpoint if endpoint else access.observe_power
        if failure is KeyboardInterrupt:
            with pytest.raises(KeyboardInterrupt) as caught:
                observe(Deadline.after(10))
            assert caught.value is retiring.close_error
        else:
            assert observe(Deadline.after(10)) == ("198.51.100.2" if endpoint else VMStatus.RUNNING)
        assert access.cleanup_incomplete == (failure is not None)
        if failure is not None:
            assert access._read_client is originals[-1]
            before = len(compute.calls) + len(network.calls)
            with pytest.raises(StateError):
                access.observe_locator(Deadline.after(10))
            assert len(compute.calls) + len(network.calls) == before and credential.closed == 0
        retiring.close_error = None
        assert access.close(Deadline.after(10)) and credential.closed == 1
        assert access._read_client is None


@pytest.mark.parametrize("selected", ["default", "principal", "browser"])
def test_real_credential_public_close_fallback_and_security_challenge(monkeypatch, selected):
    default_type, browser_type = DefaultAzureCredential, InteractiveBrowserCredential
    default_transport, selected_transport = ScriptedTransport([]), ScriptedTransport([])
    credential_originals: list[Any] = []
    browser_constructed: list[bool] = []
    token_calls: list[Any] = []
    monkeypatch.setenv("AZURE_TENANT_ID", SUB)
    monkeypatch.setenv("AZURE_CLIENT_ID", SUB)
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "offline-environment-canary")
    monkeypatch.delenv("AZURE_TOKEN_CREDENTIALS", raising=False)
    monkeypatch.setattr("agentworks.plugins.azure._owned_auth.output.info", lambda _: None)

    def default_probe(self, *scopes, **kwargs):
        raise ClientAuthenticationError("offline selected ambient probe failure")

    def token(self, *scopes, **kwargs):
        token_calls.append((scopes, kwargs))
        return AccessToken(f"offline-token-{len(token_calls)}", 9999999999)

    def token_info(self, *scopes, options=None):
        token_calls.append((scopes, dict(options or {})))
        return AccessTokenInfo(f"offline-token-{len(token_calls)}", 9999999999)

    monkeypatch.setattr(default_type, "get_token", default_probe)
    monkeypatch.setattr(ClientSecretCredential, "get_token", token)
    monkeypatch.setattr(ClientSecretCredential, "get_token_info", token_info)
    monkeypatch.setattr(browser_type, "get_token", token)
    monkeypatch.setattr(browser_type, "get_token_info", token_info)

    def construct_default(**kwargs):
        # Public exclusions isolate a real EnvironmentCredential child for public
        # close proof. This does not certify the unexcluded ambient selector.
        original = default_type(
            transport=default_transport,
            exclude_workload_identity_credential=True,
            exclude_managed_identity_credential=True,
            exclude_shared_token_cache_credential=True,
            exclude_visual_studio_code_credential=True,
            exclude_cli_credential=True,
            exclude_powershell_credential=True,
            exclude_developer_cli_credential=True,
            exclude_broker_credential=True,
            **kwargs,
        )
        credential_originals.append(original)
        return original

    def construct_browser(**kwargs):
        assert default_transport.closed == 1
        browser_constructed.append(True)
        original = browser_type(transport=selected_transport, **kwargs)
        credential_originals.append(original)
        return original

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", construct_default)
    monkeypatch.setattr("azure.identity.InteractiveBrowserCredential", construct_browser)
    auth: AzureAmbientAuth | AzureServicePrincipalAuth
    if selected == "principal":
        auth = AzureServicePrincipalAuth(mode="service-principal", tenant_id=SUB, client_id=SUB, secret="selected")

        def construct_principal(tenant, client, secret, **kwargs):
            assert secret == "offline-principal-canary"
            original = ClientSecretCredential(tenant, client, secret, transport=selected_transport, **kwargs)
            credential_originals.append(original)
            return original

        monkeypatch.setattr("azure.identity.ClientSecretCredential", construct_principal)
    else:
        auth = AzureAmbientAuth(mode="ambient")
    claims = base64.urlsafe_b64encode(b'{"access_token":{"fixture":true}}').decode().rstrip("=")
    compute = ScriptedTransport(
        [
            (401, {}, {"WWW-Authenticate": f'Bearer error="insufficient_claims", claims="{claims}"'}),
            (200, vm_payload(), {}),
        ]
    )
    bind_clients(monkeypatch, compute)
    access = AzureOwnedReadAccess(
        vm_row(), platform(auth), SimpleNamespace(secret=lambda _: "offline-principal-canary")
    )
    if selected == "default":
        default_transport.close_error = RuntimeError("offline default close failure")
        with pytest.raises(StateError):
            access.observe_power(Deadline.after(10))
        assert access._auth._credential is credential_originals[0]
        assert browser_constructed == [] and compute.calls == [] and access.cleanup_incomplete
        default_transport.close_error = None
        assert access.close(Deadline.after(10))
        assert default_transport.closed == 2 and browser_constructed == [] and compute.calls == []
    else:
        assert access.observe_power(Deadline.after(10)) is VMStatus.RUNNING
        assert [call[0] for call in compute.calls] == ["GET", "GET"]
        assert compute.calls[0][1] == compute.calls[1][1]
        assert json.loads(token_calls[-1][1]["claims"]) == {"access_token": {"fixture": True}}
        assert browser_constructed == ([True] if selected == "browser" else [])
        original = credential_originals[-1]
        assert access._auth._credential is original
        selected_transport.close_error = RuntimeError("offline selected credential close failure")
        assert not access.close(Deadline.after(10)) and access.cleanup_incomplete
        assert access._auth._credential is original
        before = list(token_calls)
        selected_transport.close_error = None
        assert access.close(Deadline.after(10)) and token_calls == before


@pytest.mark.parametrize("stage", ["compute", "nic", "pip"])
@pytest.mark.parametrize("failure", ["auth", "provider"])
@pytest.mark.parametrize("close_incomplete", [False, True])
def test_real_service_failures_detach_secret_diagnostics_and_keep_original_custody(
    monkeypatch, stage, failure, close_incomplete
):
    canary = "offline-service-credential-canary"
    body_canary = "offline-service-provider-body-canary"
    credential = Credential()
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda **kwargs: credential)
    failing_token_call = {"compute": 2, "nic": 3, "pip": 4}[stage]

    def token(*scopes, **kwargs):
        credential.calls.append((scopes, kwargs))
        if failure == "auth" and len(credential.calls) == failing_token_call:
            raise ClientAuthenticationError(canary)
        return AccessToken("offline-service-token", 9999999999)

    monkeypatch.setattr(credential, "get_token", token)
    provider_failure: Reply = (403, {"error": {"code": canary, "message": body_canary}}, {})
    compute_reply: Reply = provider_failure if stage == "compute" and failure == "provider" else (200, vm_payload(), {})
    nic_reply: Reply = (
        200,
        {"id": NIC, "properties": {"ipConfigurations": [{"properties": {"publicIPAddress": {"id": PIP}}}]}},
        {},
    )
    if stage == "nic" and failure == "provider":
        nic_reply = provider_failure
    pip_reply: Reply = (200, {"id": PIP, "properties": {"ipAddress": "198.51.100.2"}}, {})
    if stage == "pip":
        if failure == "provider":
            pip_reply = provider_failure
        else:
            # Network's successful NIC read caches its token. A real PIP
            # challenge invokes refresh again, rather than an invented probe.
            claims = base64.urlsafe_b64encode(b'{"access_token":{"fixture":true}}').decode().rstrip("=")
            pip_reply = (401, {}, {"WWW-Authenticate": f'Bearer error="insufficient_claims", claims="{claims}"'})
    compute = ScriptedTransport([compute_reply])
    network = ScriptedTransport([nic_reply, pip_reply])
    retiring = compute if stage == "compute" else network
    if close_incomplete:
        retiring.close_error = RuntimeError("offline ordinary close failure")
    originals = bind_clients(monkeypatch, compute, network)
    access = AzureOwnedReadAccess(vm_row(), platform(), SimpleNamespace(secret=deny))
    with pytest.raises(AzureError) as caught:
        if stage == "compute":
            access.observe_power(Deadline.after(10))
        else:
            access.observe_public_endpoint(Deadline.after(10))
    error = caught.value
    assert error.detail == ("ClientAuthenticationError" if failure == "auth" else "HttpResponseError")
    assert error.__cause__ is None and error.__context__ is None
    diagnostic = str(error) + error.summary + error.detail + "".join(traceback.format_exception(error))
    assert canary not in diagnostic and body_canary not in diagnostic
    assert cast(object, access._auth._credential) is credential and credential.closed == 0
    assert compute.closed == 1
    if stage != "compute":
        assert network.closed == 1
    assert access.cleanup_incomplete is close_incomplete
    if close_incomplete:
        if stage == "compute":
            assert access._read_client is originals[0]
        else:
            assert access._read_client is originals[1]
        before = len(compute.calls), len(network.calls), len(credential.calls)
        with pytest.raises(StateError):
            access.observe_power(Deadline.after(10))
        assert (len(compute.calls), len(network.calls), len(credential.calls)) == before
        retiring.close_error = None
    assert access.close(Deadline.after(10)) and credential.closed == 1
    assert retiring.closed == (2 if close_incomplete else 1)
