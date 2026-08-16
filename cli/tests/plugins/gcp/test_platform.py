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
from agentworks.capabilities.vm_platform import BootstrapProgress, ProvisionRequest, ssh_exposure
from agentworks.capabilities.vm_platform.tailscale_join import TAILSCALE_JOIN_STDIN_COMMAND
from agentworks.db import VMRow, VMStatus
from agentworks.errors import AlreadyExistsError, ConfigError, ConnectivityError, ProvisioningError, StateError
from agentworks.plugins.gcp.bootstrap import GCE_READINESS_COMMAND
from agentworks.plugins.gcp.cleanup import InstanceState, RollbackReport
from agentworks.plugins.gcp.errors import (
    GCECapacityError,
    GCEIndeterminateOperationError,
    GCEOperationError,
)
from agentworks.plugins.gcp.network import FirewallState
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

    def __init__(
        self,
        request_id: str,
        operation_type: str,
        target_link: str,
        target_id: int,
        failure: BaseException | None = None,
        *,
        status: object = None,
        error: object = None,
    ) -> None:
        self.client_operation_id = request_id
        self.operation_type = operation_type
        self.target_link = target_link
        self.target_id = target_id
        self.failure = failure
        self.status = status
        self.error = error
        self.waits: list[float] = []

    def result(self, *, timeout: float) -> None:
        self.waits.append(timeout)
        if self.failure is not None:
            raise self.failure


class _Firewalls:
    def __init__(self) -> None:
        self.resources: dict[str, compute_v1.Firewall] = {}
        self.insert_requests: list[compute_v1.InsertFirewallRequest] = []
        self.delete_requests: list[compute_v1.DeleteFirewallRequest] = []
        self.get_failures: dict[str, list[Exception]] = {}
        self.insert_failures: list[Exception | None] = []
        self.insert_wait_failures: list[BaseException | None] = []
        self.delete_failures: list[BaseException] = []
        self._next_id = 101

    def list(self, **_kwargs: object) -> list[compute_v1.Firewall]:
        return list(self.resources.values())

    def get(self, *, firewall: str, **_kwargs: object) -> compute_v1.Firewall:
        failures = self.get_failures.get(firewall, [])
        if failures:
            raise failures.pop(0)
        if firewall not in self.resources:
            raise _api_error(api_exceptions.NotFound, "provider reflection")
        return self.resources[firewall]

    def insert(self, *, request: compute_v1.InsertFirewallRequest, **_kwargs: object) -> _Operation:
        self.insert_requests.append(request)
        if self.insert_failures:
            failure = self.insert_failures.pop(0)
            if failure is not None:
                raise failure
        resource = compute_v1.Firewall(compute_v1.Firewall.to_dict(request.firewall_resource))
        resource.id = self._next_id
        self._next_id += 1
        self.resources[resource.name] = resource
        return _Operation(
            request.request_id,
            "insert",
            f"projects/{request.project}/global/firewalls/{resource.name}",
            int(resource.id),
            self.insert_wait_failures.pop(0) if self.insert_wait_failures else None,
        )

    def delete(self, *, request: compute_v1.DeleteFirewallRequest, **_kwargs: object) -> _Operation:
        self.delete_requests.append(request)
        if self.delete_failures:
            raise self.delete_failures.pop(0)
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
        self.get_failures: list[Exception] = []
        self.insert_failures: list[Exception | None] = []
        self.insert_wait_failures: list[BaseException] = []
        self.insert_operation_errors: list[object] = []
        self.delete_failures: list[Exception] = []

    def get(self, **_kwargs: object) -> compute_v1.Instance:
        if self.get_failures:
            raise self.get_failures.pop(0)
        if self.resource is None:
            raise _api_error(api_exceptions.NotFound, "provider reflection")
        return self.resource

    def insert(self, *, request: compute_v1.InsertInstanceRequest, **_kwargs: object) -> _Operation:
        self.insert_requests.append(request)
        if self.insert_failures:
            failure = self.insert_failures.pop(0)
            if failure is not None:
                raise failure
        self.resource = compute_v1.Instance(compute_v1.Instance.to_dict(request.instance_resource))
        self.resource.id = 201
        self.resource.status = "RUNNING"
        self.resource.network_interfaces[0].access_configs[0].nat_i_p = "203.0.113.19"
        return self._operation(
            request.request_id,
            "insert",
            self.insert_wait_failures.pop(0) if self.insert_wait_failures else None,
            error=self.insert_operation_errors.pop(0) if self.insert_operation_errors else None,
        )

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
        if self.delete_failures:
            raise self.delete_failures.pop(0)
        operation = self._operation(request.request_id, "delete")
        self.resource = None
        return operation

    def _operation(
        self,
        request_id: str,
        operation_type: str,
        failure: BaseException | None = None,
        *,
        error: object = None,
    ) -> _Operation:
        name = "vm-a"
        if self.resource is not None:
            name = str(self.resource.name)
        return _Operation(
            request_id,
            operation_type,
            f"projects/{_PROJECT}/zones/{_ZONE}/instances/{name}",
            201,
            failure,
            status=compute_v1.Operation.Status.DONE if error is not None else None,
            error=error,
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
                    maximum_persistent_disks=128,
                    accelerators=[],
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
    def __init__(
        self,
        *,
        fail_join: bool = False,
        failures: dict[str, list[BaseException]] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail_join = fail_join
        self.failures = failures if failures is not None else {}
        self.events = events
        self.host = ""

    def run(self, command: str, **kwargs: object) -> SSHResult:
        self.calls.append((command, kwargs))
        if self.events is not None:
            self.events.append(f"transport:{command}")
        failures = self.failures.get(command, [])
        if failures:
            raise failures.pop(0)
        if self.fail_join and kwargs.get("input_text") is not None:
            raise SSHError("fixed stdin join failed")
        stdout = "100.64.0.9\n" if command == "tailscale ip -4" else ""
        return SSHResult(returncode=0, stdout=stdout, stderr="")


class _Secrets:
    def get(self, name: str) -> str:
        assert name == "svc-json"
        return _SERVICE_SENTINEL


class _Progress:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def step(self, name: str) -> None:
        self.events.append(f"progress:{name}")

    def output(self, text: str) -> None:
        self.events.append(f"output:{text}")

    def warning(self, msg: str) -> None:
        self.events.append(f"warning:{msg}")

    def log_error(self, msg: str) -> None:
        self.events.append(f"error:{msg}")


def _ctx(*ssh_allow_cidrs: str) -> RunContext:
    return RunContext(
        config=cast("Any", SimpleNamespace(operator=SimpleNamespace(ssh_allow_cidrs=list(ssh_allow_cidrs)))),
        secrets=_Secrets(),
    )


def _request(progress: BootstrapProgress | None = None) -> ProvisionRequest:
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
    assert result.platform_metadata["allow_source_ranges"] == "198.18.0.7/32"
    assert _TAILSCALE_SENTINEL not in repr(progress.mock_calls)
    assert _SERVICE_SENTINEL not in repr(progress.mock_calls)


def test_combined_progress_precedes_readiness_and_fixed_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    transport = _Transport(events=events)
    platform, _cache = _platform(monkeypatch, transport)

    platform.create(_request(_Progress(events)), _ctx())

    combined = events.index("progress:Wait for GCE startup marker and join Tailscale through fixed stdin")
    readiness = events.index(f"transport:{GCE_READINESS_COMMAND}")
    delivery = events.index(f"transport:{TAILSCALE_JOIN_STDIN_COMMAND}")
    completed = events.index("output:GCE credential-free bootstrap and Tailscale join completed")
    assert combined < readiness < delivery < completed


def test_combined_progress_does_not_claim_completion_when_readiness_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    transport = _Transport(
        failures={GCE_READINESS_COMMAND: [SSHError("startup marker unavailable")]},
        events=events,
    )
    platform, _cache = _platform(monkeypatch, transport)

    with pytest.raises(ProvisioningError):
        platform.create(_request(_Progress(events)), _ctx())

    assert "progress:Wait for GCE startup marker and join Tailscale through fixed stdin" in events
    assert f"transport:{GCE_READINESS_COMMAND}" in events
    assert f"transport:{TAILSCALE_JOIN_STDIN_COMMAND}" not in events
    assert "output:GCE credential-free bootstrap and Tailscale join completed" not in events


def test_close_reconstructs_stable_allow_from_original_normalized_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = _platform(monkeypatch, _Transport())
    result = platform.create(_request(), _ctx("203.0.113.9"))
    vm = _vm(result.platform_metadata)
    allow_name = result.platform_metadata["allow_rule"]
    assert result.platform_metadata["allow_source_ranges"] == "198.18.0.7/32,203.0.113.9/32"

    platform.post_tailscale_ready(vm, _ctx("192.0.2.44/32"))

    assert allow_name not in cache.firewalls.resources


@pytest.mark.parametrize(
    "failure_stage",
    ("deny-insert", "allow-insert", "instance-insert", "readiness"),
)
def test_create_failures_clean_every_partial_resource_set(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    provider_failure = _api_error(api_exceptions.ServiceUnavailable, "provider-private-detail")
    transport_failures: dict[str, list[BaseException]] = {}
    transport = _Transport(failures=transport_failures)
    platform, cache = _platform(monkeypatch, transport)
    if failure_stage == "deny-insert":
        cache.firewalls.insert_failures = [provider_failure, provider_failure]
    elif failure_stage == "allow-insert":
        cache.firewalls.insert_failures = [None, provider_failure, provider_failure]
    elif failure_stage == "instance-insert":
        cache.instances.insert_failures = [provider_failure, provider_failure]
    else:
        transport_failures[GCE_READINESS_COMMAND] = [SSHError("startup marker unavailable")]

    with pytest.raises((ConnectivityError, ProvisioningError)):
        platform.create(_request(), _ctx())

    assert cache.instances.resource is None
    assert cache.firewalls.resources == {}
    assert not any(kwargs.get("input_text") for _command, kwargs in transport.calls)
    if failure_stage == "readiness":
        assert len(cache.instances.delete_requests) == 1


@pytest.mark.parametrize("interrupt_stage", ("deny-wait", "allow-wait", "instance-wait"))
def test_first_interrupt_carries_operation_ownership_through_platform_rollback(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_stage: str,
) -> None:
    platform, cache = _platform(monkeypatch, _Transport())
    if interrupt_stage == "deny-wait":
        cache.firewalls.insert_wait_failures = [KeyboardInterrupt(interrupt_stage)]
    elif interrupt_stage == "allow-wait":
        cache.firewalls.insert_wait_failures = [None, KeyboardInterrupt(interrupt_stage)]
    else:
        cache.instances.insert_wait_failures = [KeyboardInterrupt(interrupt_stage)]

    with pytest.raises(KeyboardInterrupt, match=interrupt_stage):
        platform.create(_request(), _ctx())

    assert cache.instances.resource is None
    assert cache.firewalls.resources == {}
    if interrupt_stage == "deny-wait":
        assert [request.firewall for request in cache.firewalls.delete_requests] == [
            cache.firewalls.insert_requests[0].firewall_resource.name
        ]
    elif interrupt_stage == "allow-wait":
        assert len(cache.firewalls.delete_requests) == 2
    else:
        assert len(cache.instances.delete_requests) == 1
        assert len(cache.firewalls.delete_requests) == 2


def test_ordinary_failure_cleanup_interrupt_reenters_idempotent_platform_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = KeyboardInterrupt("first")
    transport = _Transport(failures={TAILSCALE_JOIN_STDIN_COMMAND: [SSHError(_SERVICE_SENTINEL)]})
    platform, cache = _platform(monkeypatch, transport)
    rollback_calls = 0

    def partial_then_complete_rollback(**kwargs: object) -> RollbackReport:
        nonlocal rollback_calls
        rollback_calls += 1
        if rollback_calls == 1:
            expected_allow = cast("compute_v1.Firewall", kwargs["expected_allow"])
            cache.firewalls.resources.pop(str(expected_allow.name))
            raise first
        cache.instances.resource = None
        cache.firewalls.resources.clear()
        return RollbackReport(InstanceState.ABSENT, FirewallState.ABSENT, FirewallState.ABSENT)

    monkeypatch.setattr(
        "agentworks.plugins.gcp.platform.rollback_partial_create",
        partial_then_complete_rollback,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        platform.create(_request(), _ctx())

    assert caught.value is first
    assert first.__cause__ is None
    assert first.__context__ is None
    assert _SERVICE_SENTINEL not in repr(first)
    assert rollback_calls == 2
    assert cache.instances.resource is None
    assert cache.firewalls.resources == {}


def test_second_interrupt_abandons_platform_rollback_without_mutating_survivors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.cleanup.output.warn", warnings.append)
    transport = _Transport(failures={GCE_READINESS_COMMAND: [KeyboardInterrupt("first")]})
    platform, cache = _platform(monkeypatch, transport)
    cache.firewalls.delete_failures = [KeyboardInterrupt("second")]

    with pytest.raises(KeyboardInterrupt, match="first"):
        platform.create(_request(), _ctx())

    assert cache.instances.resource is not None
    assert len(cache.firewalls.resources) == 2
    assert cache.instances.delete_requests == []
    message = "\n".join(warnings)
    assert "Cleanup abandoned" in message
    assert "firewall-rules delete" not in message


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


@pytest.mark.parametrize(
    "wait_failure",
    [
        GCEOperationError("definitive"),
        GCECapacityError("capacity"),
        GCEIndeterminateOperationError("indeterminate"),
    ],
    ids=("definitive", "capacity", "indeterminate"),
)
def test_power_operations_propagate_every_wait_failure(
    monkeypatch: pytest.MonkeyPatch,
    wait_failure: GCEOperationError | GCECapacityError | GCEIndeterminateOperationError,
) -> None:
    platform, _cache = _platform(monkeypatch, _Transport())
    vm = _vm(platform.create(_request(), _ctx()).platform_metadata)

    def fail_wait(*_args: object, **_kwargs: object) -> None:
        raise wait_failure

    monkeypatch.setattr("agentworks.plugins.gcp.errors.wait_for_extended_operation", fail_wait)

    with pytest.raises(type(wait_failure)) as caught:
        platform.stop(vm, _ctx())

    assert caught.value is wait_failure


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


@pytest.mark.parametrize(
    "machine",
    [
        compute_v1.MachineType(
            name="e2-standard-2",
            self_link=f"projects/{_PROJECT}/zones/{_ZONE}/machineTypes/e2-standard-2",
            guest_cpus=2,
            memory_mb=8192,
            architecture="x86_64",
            maximum_persistent_disks=0,
            accelerators=[],
        ),
        compute_v1.MachineType(
            name="e2-standard-2",
            self_link=f"projects/{_PROJECT}/zones/{_ZONE}/machineTypes/e2-standard-2",
            guest_cpus=2,
            memory_mb=8192,
            architecture="x86_64",
            maximum_persistent_disks=128,
            accelerators=[compute_v1.Accelerators(guest_accelerator_count=1, guest_accelerator_type="required")],
        ),
    ],
    ids=("present-zero-persistent-disk", "required-accelerator"),
)
def test_known_machine_incompatibility_stays_pre_mutation(
    monkeypatch: pytest.MonkeyPatch,
    machine: compute_v1.MachineType,
) -> None:
    platform, cache = _platform(monkeypatch, _Transport())
    cache.clients["machine-types"] = SimpleNamespace(get=lambda **_kwargs: machine)

    with pytest.raises(ConfigError, match="e2-standard-2"):
        platform.create(_request(), _ctx())

    assert cache.firewalls.insert_requests == []
    assert cache.instances.insert_requests == []


@pytest.mark.parametrize(
    ("machine", "message"),
    [
        (
            compute_v1.MachineType(memory_mb=8192, architecture="x86_64", maximum_persistent_disks=128),
            "unknown provider shape.*guest_cpus",
        ),
        (
            compute_v1.MachineType(
                guest_cpus=0,
                memory_mb=8192,
                architecture="x86_64",
                maximum_persistent_disks=128,
            ),
            "invalid provider shape.*guest_cpus",
        ),
        (
            compute_v1.MachineType(
                guest_cpus=4,
                memory_mb=8192,
                architecture="x86_64",
                maximum_persistent_disks=128,
            ),
            "does not match",
        ),
        (
            compute_v1.MachineType(guest_cpus=2, architecture="x86_64", maximum_persistent_disks=128),
            "unknown provider shape.*memory_mb",
        ),
        (
            compute_v1.MachineType(
                guest_cpus=2,
                memory_mb=0,
                architecture="x86_64",
                maximum_persistent_disks=128,
            ),
            "invalid provider shape.*memory_mb",
        ),
        (
            compute_v1.MachineType(
                guest_cpus=2,
                memory_mb=16384,
                architecture="x86_64",
                maximum_persistent_disks=128,
            ),
            "does not match",
        ),
    ],
    ids=(
        "cpu-absent",
        "cpu-non-positive",
        "cpu-declaration-mismatch",
        "memory-absent",
        "memory-non-positive",
        "memory-declaration-mismatch",
    ),
)
def test_required_machine_shape_failure_has_zero_provider_mutations(
    monkeypatch: pytest.MonkeyPatch,
    machine: compute_v1.MachineType,
    message: str,
) -> None:
    platform, cache = _platform(monkeypatch, _Transport())
    cache.clients["machine-types"] = SimpleNamespace(get=lambda **_kwargs: machine)

    with pytest.raises(ConfigError, match=message):
        platform.create(_request(), _ctx())

    assert cache.firewalls.insert_requests == []
    assert cache.instances.insert_requests == []


def test_residual_definitive_insert_rejection_rolls_back_and_stays_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = _platform(monkeypatch, _Transport())
    provider_sentinel = "provider-private-insert-SENTINEL"
    provider = _api_error(api_exceptions.ServiceUnavailable, provider_sentinel)
    provider.__cause__ = RuntimeError(provider_sentinel)
    cache.instances.insert_wait_failures = [provider]
    cache.instances.insert_operation_errors = [
        compute_v1.Error(errors=[compute_v1.Errors(code="UNKNOWN", message=provider_sentinel)])
    ]

    with pytest.raises(GCEOperationError) as caught:
        platform.create(_request(), _ctx())

    assert type(caught.value) is GCEOperationError
    assert "e2-standard-2" in str(caught.value)
    assert "IAM, quota, and request prerequisites first" in (caught.value.hint or "")
    assert "CPU-only Debian 12" in (caught.value.hint or "")
    assert "pd-balanced" in (caught.value.hint or "")
    assert cache.instances.resource is None
    assert len(cache.instances.delete_requests) == 1
    assert cache.firewalls.resources == {}
    assert provider_sentinel not in f"{caught.value!s} {caught.value!r} {vars(caught.value)!r}"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


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


def test_delete_retains_same_id_mutated_allow_but_still_deletes_instance_and_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.platform.output.warn", warnings.append)
    platform, cache = _platform(monkeypatch, _Transport())
    result = platform.create(_request(), _ctx())
    vm = _vm(result.platform_metadata)
    allow_name = result.platform_metadata["allow_rule"]
    deny_name = result.platform_metadata["deny_rule"]
    mutated = cache.firewalls.resources[allow_name]
    mutated.priority = 7

    platform.delete(vm, _ctx())

    assert cache.instances.resource is None
    assert allow_name in cache.firewalls.resources
    assert deny_name not in cache.firewalls.resources
    assert [request.firewall for request in cache.firewalls.delete_requests] == [deny_name]
    message = "\n".join(warnings)
    assert "shape mismatch" in message
    assert "do not delete by name" in message


def test_delete_refuses_noncanonical_persisted_allow_sources_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = _platform(monkeypatch, _Transport())
    result = platform.create(_request(), _ctx())
    metadata = dict(result.platform_metadata)
    metadata["allow_source_ranges"] = "198.18.0.7"

    with pytest.raises(StateError, match="invalid persisted GCE allow-source metadata"):
        platform.delete(_vm(metadata), _ctx())

    assert cache.instances.delete_requests == []
    assert cache.firewalls.delete_requests == []


@pytest.mark.parametrize("provider_error", (api_exceptions.ServiceUnavailable, api_exceptions.PermissionDenied))
def test_delete_continues_after_allow_read_failure_and_reports_retained_allow(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: type[Exception],
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.platform.output.warn", warnings.append)
    platform, cache = _platform(monkeypatch, _Transport())
    result = platform.create(_request(), _ctx())
    vm = _vm(result.platform_metadata)
    allow_name = result.platform_metadata["allow_rule"]
    deny_name = result.platform_metadata["deny_rule"]
    cache.firewalls.get_failures[allow_name] = [_api_error(provider_error, "provider-private-detail")]

    platform.delete(vm, _ctx())

    assert len(cache.instances.delete_requests) == 1
    assert cache.instances.resource is None
    assert allow_name in cache.firewalls.resources
    assert deny_name not in cache.firewalls.resources
    assert all(request.firewall != allow_name for request in cache.firewalls.delete_requests)
    message = "\n".join(warnings)
    assert "Allow rule ownership is unknown" in message
    assert "do not delete by name" in message
    assert "provider-private-detail" not in message


def test_delete_keeps_deny_when_allow_read_and_instance_delete_are_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = _platform(monkeypatch, _Transport())
    result = platform.create(_request(), _ctx())
    vm = _vm(result.platform_metadata)
    allow_name = result.platform_metadata["allow_rule"]
    deny_name = result.platform_metadata["deny_rule"]
    unavailable = _api_error(api_exceptions.ServiceUnavailable, "provider-private-detail")
    cache.firewalls.get_failures[allow_name] = [unavailable]
    cache.instances.delete_failures = [unavailable]

    with pytest.raises(GCEOperationError, match="not proven absent") as caught:
        platform.delete(vm, _ctx())

    assert len(cache.instances.delete_requests) == 1
    assert cache.instances.resource is not None
    assert {allow_name, deny_name} <= set(cache.firewalls.resources)
    assert all(request.firewall != deny_name for request in cache.firewalls.delete_requests)
    assert "Owned deny rule retained while instance absence is unproven" in (caught.value.hint or "")
    assert "provider-private-detail" not in (caught.value.hint or "")


@pytest.mark.parametrize("hook_name", ("post_tailscale_ready", "secure_failed_vm"))
def test_stable_allow_close_hooks_fail_closed_on_provider_read_error(
    monkeypatch: pytest.MonkeyPatch,
    hook_name: str,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.platform.output.warn", warnings.append)
    platform, cache = _platform(monkeypatch, _Transport())
    result = platform.create(_request(), _ctx())
    vm = _vm(result.platform_metadata)
    allow_name = result.platform_metadata["allow_rule"]
    cache.firewalls.get_failures[allow_name] = [
        _api_error(api_exceptions.ServiceUnavailable, "provider-private-detail")
    ]

    getattr(platform, hook_name)(vm, _ctx())

    assert allow_name in cache.firewalls.resources
    assert all(request.firewall != allow_name for request in cache.firewalls.delete_requests)
    assert "not proven absent" in "\n".join(warnings)
    assert "provider-private-detail" not in "\n".join(warnings)


def test_transient_route_open_failure_never_yields_or_mutates_stable_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = _platform(monkeypatch, _Transport())
    result = platform.create(_request(), _ctx())
    vm = _vm(result.platform_metadata)
    platform.post_tailscale_ready(vm, _ctx())
    unavailable_one = _api_error(api_exceptions.ServiceUnavailable, "first-private-detail")
    unavailable_two = _api_error(api_exceptions.ServiceUnavailable, "second-private-detail")
    cache.firewalls.insert_failures = [unavailable_one, unavailable_two]
    yielded = False

    with pytest.raises(ConnectivityError), platform.transient_route(vm, _ctx()):
        yielded = True

    assert not yielded
    assert set(cache.firewalls.resources) == {result.platform_metadata["deny_rule"]}
    first, second = cache.firewalls.insert_requests[-2:]
    assert first.request_id == second.request_id


def test_transient_route_close_failure_warns_and_retains_only_its_exact_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.platform.output.warn", warnings.append)
    platform, cache = _platform(monkeypatch, _Transport())
    result = platform.create(_request(), _ctx())
    vm = _vm(result.platform_metadata)
    platform.post_tailscale_ready(vm, _ctx())
    cache.firewalls.delete_failures = [_api_error(api_exceptions.ServiceUnavailable, "provider-private-detail")]

    with platform.transient_route(vm, _ctx()):
        [route_name] = set(cache.firewalls.resources) - {result.platform_metadata["deny_rule"]}

    assert {result.platform_metadata["deny_rule"], route_name} == set(cache.firewalls.resources)
    message = "\n".join(warnings)
    assert route_name in message
    assert "not proven absent" in message
    assert "provider-private-detail" not in message
