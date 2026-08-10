"""Offline full-create, lifecycle, and transient-route behavior for GCE."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import compute_v1

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest, ssh_exposure
from agentworks.capabilities.vm_platform.tailscale_join import TAILSCALE_JOIN_STDIN_COMMAND
from agentworks.db import VMRow, VMStatus
from agentworks.errors import AlreadyExistsError
from agentworks.plugins.gcp.bootstrap import GCE_READINESS_COMMAND
from agentworks.plugins.gcp.errors import GCEOperationError
from agentworks.plugins.gcp.platform import GCEPlatform
from agentworks.ssh import SSHError, SSHResult

_TAILSCALE_SENTINEL = "tskey-hostile-'\"$()"
_SERVICE_SENTINEL = '{"private_key":"service-hostile-\'\\"$()"}'
_PROJECT = "project-a"
_ZONE = "us-central1-a"
_NETWORK = f"projects/{_PROJECT}/global/networks/default"


def _api_error(kind: type[Exception], message: str) -> Exception:
    return cast("Callable[[str], Exception]", kind)(message)


class _Operation:
    error_code = None

    def __init__(self, request_id: str, operation_type: str, target_link: str, target_id: int) -> None:
        self.client_operation_id = request_id
        self.operation_type = operation_type
        self.target_link = target_link
        self.target_id = target_id
        self.waits: list[float] = []

    def result(self, *, timeout: float) -> None:
        self.waits.append(timeout)


class _Firewalls:
    def __init__(self) -> None:
        self.resources: dict[str, compute_v1.Firewall] = {}
        self.insert_requests: list[compute_v1.InsertFirewallRequest] = []
        self.delete_requests: list[compute_v1.DeleteFirewallRequest] = []
        self._next_id = 101

    def list(self, **_kwargs: object) -> list[compute_v1.Firewall]:
        return list(self.resources.values())

    def get(self, *, firewall: str, **_kwargs: object) -> compute_v1.Firewall:
        if firewall not in self.resources:
            raise _api_error(api_exceptions.NotFound, "provider reflection")
        return self.resources[firewall]

    def insert(self, *, request: compute_v1.InsertFirewallRequest, **_kwargs: object) -> _Operation:
        self.insert_requests.append(request)
        resource = compute_v1.Firewall(compute_v1.Firewall.to_dict(request.firewall_resource))
        resource.id = self._next_id
        self._next_id += 1
        self.resources[resource.name] = resource
        return _Operation(
            request.request_id,
            "insert",
            f"projects/{request.project}/global/firewalls/{resource.name}",
            int(resource.id),
        )

    def delete(self, *, request: compute_v1.DeleteFirewallRequest, **_kwargs: object) -> _Operation:
        self.delete_requests.append(request)
        resource = self.resources.pop(request.firewall)
        return _Operation(
            request.request_id,
            "delete",
            f"projects/{request.project}/global/firewalls/{request.firewall}",
            int(resource.id),
        )


class _Instances:
    def __init__(self) -> None:
        self.resource: compute_v1.Instance | None = None
        self.insert_requests: list[compute_v1.InsertInstanceRequest] = []
        self.start_requests: list[compute_v1.StartInstanceRequest] = []
        self.stop_requests: list[compute_v1.StopInstanceRequest] = []
        self.delete_requests: list[compute_v1.DeleteInstanceRequest] = []

    def get(self, **_kwargs: object) -> compute_v1.Instance:
        if self.resource is None:
            raise _api_error(api_exceptions.NotFound, "provider reflection")
        return self.resource

    def insert(self, *, request: compute_v1.InsertInstanceRequest, **_kwargs: object) -> _Operation:
        self.insert_requests.append(request)
        self.resource = compute_v1.Instance(compute_v1.Instance.to_dict(request.instance_resource))
        self.resource.id = 201
        self.resource.status = "RUNNING"
        self.resource.network_interfaces[0].access_configs[0].nat_i_p = "203.0.113.19"
        return self._operation(request.request_id, "insert")

    def start(self, *, request: compute_v1.StartInstanceRequest, **_kwargs: object) -> _Operation:
        self.start_requests.append(request)
        assert self.resource is not None
        self.resource.status = "RUNNING"
        self.resource.network_interfaces[0].access_configs[0].nat_i_p = "203.0.113.20"
        return self._operation(request.request_id, "start")

    def stop(self, *, request: compute_v1.StopInstanceRequest, **_kwargs: object) -> _Operation:
        self.stop_requests.append(request)
        assert self.resource is not None
        self.resource.status = "TERMINATED"
        return self._operation(request.request_id, "stop")

    def delete(self, *, request: compute_v1.DeleteInstanceRequest, **_kwargs: object) -> _Operation:
        self.delete_requests.append(request)
        operation = self._operation(request.request_id, "delete")
        self.resource = None
        return operation

    def _operation(self, request_id: str, operation_type: str) -> _Operation:
        name = "vm-a"
        if self.resource is not None:
            name = str(self.resource.name)
        return _Operation(
            request_id,
            operation_type,
            f"projects/{_PROJECT}/zones/{_ZONE}/instances/{name}",
            201,
        )


class _Cache:
    def __init__(self, firewalls: _Firewalls, instances: _Instances) -> None:
        self.firewalls = firewalls
        self.instances = instances
        self.secret_was_delivered = False
        self.clients: dict[str, object] = {
            "projects": SimpleNamespace(get=lambda **_kwargs: SimpleNamespace(name=_PROJECT)),
            "zones": SimpleNamespace(get=lambda **_kwargs: SimpleNamespace(name=_ZONE)),
            "networks": SimpleNamespace(
                get=lambda **_kwargs: compute_v1.Network(
                    name="default",
                    self_link=_NETWORK,
                    network_firewall_policy_enforcement_order="AFTER_CLASSIC_FIREWALL",
                )
            ),
            "machine-types": SimpleNamespace(
                get=lambda **_kwargs: compute_v1.MachineType(
                    name="e2-standard-2",
                    self_link=f"projects/{_PROJECT}/zones/{_ZONE}/machineTypes/e2-standard-2",
                    guest_cpus=2,
                    memory_mb=8192,
                    architecture="x86_64",
                )
            ),
            "images": SimpleNamespace(
                get_from_family=lambda **_kwargs: compute_v1.Image(
                    name="debian-12-v1",
                    self_link="projects/debian-cloud/global/images/debian-12-v1",
                    architecture="x86_64",
                )
            ),
            "disk-types": SimpleNamespace(
                get=lambda **_kwargs: compute_v1.DiskType(
                    name="pd-balanced",
                    self_link=f"projects/{_PROJECT}/zones/{_ZONE}/diskTypes/pd-balanced",
                )
            ),
            "firewalls": firewalls,
            "instances": instances,
        }

    def client(self, kind: str, ctx: RunContext) -> Any:
        if not self.secret_was_delivered:
            assert ctx.secret("svc-json") == _SERVICE_SENTINEL
            self.secret_was_delivered = True
        return self.clients[kind]


class _Transport:
    def __init__(self, *, fail_join: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_join = fail_join
        self.host = ""

    def run(self, command: str, **kwargs: object) -> SSHResult:
        self.calls.append((command, kwargs))
        if self.fail_join and kwargs.get("input_text") is not None:
            raise SSHError("fixed stdin join failed")
        stdout = "100.64.0.9\n" if command == "tailscale ip -4" else ""
        return SSHResult(returncode=0, stdout=stdout, stderr="")


class _Secrets:
    def get(self, name: str) -> str:
        assert name == "svc-json"
        return _SERVICE_SENTINEL


def _ctx() -> RunContext:
    return RunContext(
        config=cast("Any", SimpleNamespace(operator=SimpleNamespace(ssh_allow_cidrs=[]))),
        secrets=_Secrets(),
    )


def _request(progress: MagicMock | None = None) -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="a",
        hostname="vm-a",
        system_slug=None,
        admin_username="agentworks",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=None,
        tailscale_auth_key=_TAILSCALE_SENTINEL,
        progress=progress or MagicMock(),
        cpus=2,
        memory_gib=8,
        disk_gib=40,
        swap_gib=4,
    )


def _platform(monkeypatch: pytest.MonkeyPatch, transport: _Transport) -> tuple[GCEPlatform, _Cache]:
    platform = GCEPlatform(
        "gcp-site",
        {
            "name": "gcp-gce",
            "project_id": _PROJECT,
            "zone": _ZONE,
            "auth": {"mode": "service-account", "secret": "svc-json"},
        },
    )
    cache = _Cache(_Firewalls(), _Instances())
    platform._clients = cache  # type: ignore[assignment]

    def transport_factory(*_args: object, **kwargs: object) -> _Transport:
        transport.host = str(kwargs["host"])
        return transport

    monkeypatch.setattr("agentworks.plugins.gcp.platform.SSHTransport", transport_factory)
    return platform, cache


@pytest.fixture(autouse=True)
def _egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_exposure, "_egress_ip_cache", None)
    monkeypatch.setattr(ssh_exposure, "detect_egress_ip", lambda: "198.18.0.7")


def _vm(result_metadata: dict[str, str]) -> VMRow:
    return VMRow(
        name="a",
        site="gcp-site",
        template=None,
        admin_template=None,
        extra_packages=[],
        provisioning_status="complete",
        init_status="complete",
        tailscale_host=None,
        cpus=2,
        memory_gib=8,
        disk_gib=40,
        swap_gib=4,
        admin_username="agentworks",
        hostname="vm-a",
        created_at="now",
        last_seen_at=None,
        platform_metadata=result_metadata,
    )


def test_full_create_retains_secret_free_request_and_joins_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport()
    platform, cache = _platform(monkeypatch, transport)
    progress = MagicMock()
    result = platform.create(_request(progress), _ctx())

    assert cache.secret_was_delivered
    [request] = cache.instances.insert_requests
    body = compute_v1.Instance.to_dict(request.instance_resource)
    serialized = repr(body)
    assert _TAILSCALE_SENTINEL not in serialized
    assert _SERVICE_SENTINEL not in serialized
    assert body["service_accounts"] == []
    assert body["disks"][0]["auto_delete"] is True
    assert body["network_interfaces"][0]["stack_type"] == "IPV4_ONLY"
    metadata = {item["key"]: item["value"] for item in body["metadata"]["items"]}
    assert _TAILSCALE_SENTINEL not in metadata["startup-script"]
    assert request.request_id
    sensitive = [(command, kwargs) for command, kwargs in transport.calls if kwargs.get("input_text") is not None]
    assert sensitive == [
        (
            TAILSCALE_JOIN_STDIN_COMMAND,
            {"sudo": True, "timeout": 30, "input_text": f"{_TAILSCALE_SENTINEL}\n"},
        )
    ]
    assert GCE_READINESS_COMMAND in [command for command, _kwargs in transport.calls]
    assert result.tailscale_ip == "100.64.0.9"
    assert "203.0.113.19" not in repr(result.platform_metadata)
    assert "198.18.0.7" not in repr(result.platform_metadata)
    assert _TAILSCALE_SENTINEL not in repr(progress.mock_calls)
    assert _SERVICE_SENTINEL not in repr(progress.mock_calls)


def test_lifecycle_is_idempotent_live_ip_and_transient_routes_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport()
    platform, cache = _platform(monkeypatch, transport)
    result = platform.create(_request(), _ctx())
    vm = _vm(result.platform_metadata)
    firewalls = cache.firewalls
    instances = cache.instances

    platform.post_tailscale_ready(vm, _ctx())
    assert result.platform_metadata["allow_rule"] not in firewalls.resources
    assert platform.status(vm, _ctx()) is VMStatus.RUNNING
    platform.stop(vm, _ctx())
    platform.stop(vm, _ctx())
    assert len(instances.stop_requests) == 1
    assert platform.status(vm, _ctx()) is VMStatus.STOPPED
    platform.start(vm, _ctx())
    platform.start(vm, _ctx())
    assert len(instances.start_requests) == 1
    native = platform.native_transport(vm, _ctx())
    assert native is not None
    assert cast("Any", native).host == "203.0.113.20"

    with platform.transient_route(vm, _ctx()):
        route_one = set(firewalls.resources) - {result.platform_metadata["deny_rule"]}
        assert len(route_one) == 1
        with platform.transient_route(vm, _ctx()):
            routes_both = set(firewalls.resources) - {result.platform_metadata["deny_rule"]}
            assert len(routes_both) == 2
            assert route_one < routes_both
        assert (set(firewalls.resources) - {result.platform_metadata["deny_rule"]}) == route_one
    assert set(firewalls.resources) == {result.platform_metadata["deny_rule"]}

    platform.delete(vm, _ctx())
    platform.delete(vm, _ctx())
    assert instances.resource is None
    assert firewalls.resources == {}
    assert len(instances.delete_requests) == 1


def test_join_failure_rolls_back_and_exception_graph_is_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(fail_join=True)
    platform, cache = _platform(monkeypatch, transport)
    with pytest.raises(SSHError) as caught:
        platform.create(_request(), _ctx())
    assert cache.instances.resource is None
    assert cache.firewalls.resources == {}
    pending: list[BaseException | None] = [caught.value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        assert _TAILSCALE_SENTINEL not in str(current)
        assert _SERVICE_SENTINEL not in str(current)
        pending.extend((current.__cause__, current.__context__))


def test_runup_is_authenticated_read_only_and_create_collision_stays_p0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = _platform(monkeypatch, _Transport())
    platform.runup(_ctx())
    assert cache.secret_was_delivered
    assert cache.firewalls.insert_requests == []
    assert cache.instances.insert_requests == []

    cache.instances.resource = compute_v1.Instance(id=999, name="vm-a")
    with pytest.raises(AlreadyExistsError):
        platform.create(_request(), _ctx())
    assert cache.firewalls.insert_requests == []
    assert cache.instances.insert_requests == []


def test_delete_retains_same_name_different_id_instance_and_lifetime_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = _platform(monkeypatch, _Transport())
    result = platform.create(_request(), _ctx())
    vm = _vm(result.platform_metadata)
    assert cache.instances.resource is not None
    cache.instances.resource.id = 999

    with pytest.raises(GCEOperationError, match="not proven absent") as caught:
        platform.delete(vm, _ctx())
    assert cache.instances.resource is not None
    assert cache.instances.delete_requests == []
    assert result.platform_metadata["deny_rule"] in cache.firewalls.resources
    assert "do not delete by name" in (caught.value.hint or "")
