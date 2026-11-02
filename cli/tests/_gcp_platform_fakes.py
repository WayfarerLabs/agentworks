"""Offline fakes for GCE platform tests: the compute clients, a create request and a platform wired to them."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import compute_v1

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import BootstrapProgress, ProvisionRequest
from agentworks.capabilities.vm_platform.tailscale_join import JOIN_FRAGMENT
from agentworks.debian import DebianRelease
from agentworks.plugins.gcp.platform import GCEPlatform
from tests._root_channel_fakes import Answer, FakeGuest, answer_by_payload, install_at_run_process
from tests.conftest import provisioning_trust_for_tests

TAILSCALE_SENTINEL = "tskey-hostile-'\"$()"
SERVICE_SENTINEL = '{"private_key":"service-hostile-\'\\"$()"}'
PROJECT = "project-a"
ZONE = "us-central1-a"
NETWORK = f"projects/{PROJECT}/global/networks/default"


def api_error(kind: type[Exception], message: str) -> Exception:
    return cast("Callable[[str], Exception]", kind)(message)


class FakeOperation:
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


class FakeFirewalls:
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
            raise api_error(api_exceptions.NotFound, "provider reflection")
        return self.resources[firewall]

    def insert(self, *, request: compute_v1.InsertFirewallRequest, **_kwargs: object) -> FakeOperation:
        self.insert_requests.append(request)
        if self.insert_failures:
            failure = self.insert_failures.pop(0)
            if failure is not None:
                raise failure
        resource = compute_v1.Firewall(compute_v1.Firewall.to_dict(request.firewall_resource))
        resource.id = self._next_id
        self._next_id += 1
        self.resources[resource.name] = resource
        return FakeOperation(
            request.request_id,
            "insert",
            f"projects/{request.project}/global/firewalls/{resource.name}",
            int(resource.id),
            self.insert_wait_failures.pop(0) if self.insert_wait_failures else None,
        )

    def delete(self, *, request: compute_v1.DeleteFirewallRequest, **_kwargs: object) -> FakeOperation:
        self.delete_requests.append(request)
        if self.delete_failures:
            raise self.delete_failures.pop(0)
        resource = self.resources.pop(request.firewall)
        return FakeOperation(
            request.request_id,
            "delete",
            f"projects/{request.project}/global/firewalls/{request.firewall}",
            int(resource.id),
        )


class FakeInstances:
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
        self.get_calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> compute_v1.Instance:
        self.get_calls.append(kwargs)
        if self.get_failures:
            raise self.get_failures.pop(0)
        if self.resource is None:
            raise api_error(api_exceptions.NotFound, "provider reflection")
        return self.resource

    def insert(self, *, request: compute_v1.InsertInstanceRequest, **_kwargs: object) -> FakeOperation:
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

    def start(self, *, request: compute_v1.StartInstanceRequest, **_kwargs: object) -> FakeOperation:
        self.start_requests.append(request)
        assert self.resource is not None
        self.resource.status = "RUNNING"
        self.resource.network_interfaces[0].access_configs[0].nat_i_p = "203.0.113.20"
        return self._operation(request.request_id, "start")

    def stop(self, *, request: compute_v1.StopInstanceRequest, **_kwargs: object) -> FakeOperation:
        self.stop_requests.append(request)
        assert self.resource is not None
        self.resource.status = "TERMINATED"
        return self._operation(request.request_id, "stop")

    def delete(self, *, request: compute_v1.DeleteInstanceRequest, **_kwargs: object) -> FakeOperation:
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
    ) -> FakeOperation:
        name = "vm-a"
        if self.resource is not None:
            name = str(self.resource.name)
        return FakeOperation(
            request_id,
            operation_type,
            f"projects/{PROJECT}/zones/{ZONE}/instances/{name}",
            201,
            failure,
            status=compute_v1.Operation.Status.DONE if error is not None else None,
            error=error,
        )


class FakeClients:
    def __init__(self, firewalls: FakeFirewalls, instances: FakeInstances) -> None:
        self.firewalls = firewalls
        self.instances = instances
        self.secret_was_delivered = False
        self.clients: dict[str, object] = {
            "projects": SimpleNamespace(get=lambda **_kwargs: SimpleNamespace(name=PROJECT)),
            "zones": SimpleNamespace(get=lambda **_kwargs: SimpleNamespace(name=ZONE)),
            "networks": SimpleNamespace(
                get=lambda **_kwargs: compute_v1.Network(
                    name="default",
                    self_link=NETWORK,
                    network_firewall_policy_enforcement_order="AFTER_CLASSIC_FIREWALL",
                )
            ),
            "machine-types": SimpleNamespace(
                get=lambda **_kwargs: compute_v1.MachineType(
                    name="e2-standard-2",
                    self_link=f"projects/{PROJECT}/zones/{ZONE}/machineTypes/e2-standard-2",
                    guest_cpus=2,
                    memory_mb=8192,
                    architecture="x86_64",
                    maximum_persistent_disks=128,
                    accelerators=[],
                )
            ),
            "images": SimpleNamespace(
                get_from_family=lambda **_kwargs: compute_v1.Image(
                    name="debian-13-v1",
                    self_link="projects/debian-cloud/global/images/debian-13-v1",
                    architecture="x86_64",
                )
            ),
            "disk-types": SimpleNamespace(
                get=lambda **_kwargs: compute_v1.DiskType(
                    name="pd-balanced",
                    self_link=f"projects/{PROJECT}/zones/{ZONE}/diskTypes/pd-balanced",
                )
            ),
            "firewalls": firewalls,
            "instances": instances,
        }

    def client(self, kind: str, ctx: RunContext) -> Any:
        if not self.secret_was_delivered:
            assert ctx.secret("svc-json") == SERVICE_SENTINEL
            self.secret_was_delivered = True
        return self.clients[kind]


def gce_guest(
    *,
    fail_join: bool = False,
    failures: dict[str, list[BaseException]] | None = None,
    events: list[str] | None = None,
) -> FakeGuest:
    """The VM behind the root channel; ``fail_join`` makes ``tailscale up`` exit 1."""
    answers = {JOIN_FRAGMENT: Answer(status=1)} if fail_join else {}
    return FakeGuest(answer_by_payload(failures=failures, answers=answers), events=events)


class FakeSecrets:
    def get(self, name: str) -> str:
        assert name == "svc-json"
        return SERVICE_SENTINEL


class RecordingProgress:
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


def gce_ctx(*ssh_allow_cidrs: str) -> RunContext:
    return RunContext(
        config=cast("Any", SimpleNamespace(operator=SimpleNamespace(ssh_allow_cidrs=list(ssh_allow_cidrs)))),
        secrets=FakeSecrets(),
    )


def gce_request(progress: BootstrapProgress | None = None) -> ProvisionRequest:
    return ProvisionRequest(
        vm_name="a",
        debian_release=DebianRelease.TRIXIE,
        hostname="vm-a",
        system_slug=None,
        admin_username="agentworks",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        provisioning_trust=provisioning_trust_for_tests("a"),
        tailscale_auth_key=TAILSCALE_SENTINEL,
        progress=progress or MagicMock(),
        cpus=2,
        memory_gib=8,
        disk_gib=40,
        swap_gib=4,
    )


def gce_platform(monkeypatch: pytest.MonkeyPatch, guest: FakeGuest) -> tuple[GCEPlatform, FakeClients]:
    platform = GCEPlatform(
        "gcp-site",
        {
            "name": "gcp-gce",
            "project_id": PROJECT,
            "zone": ZONE,
            "auth": {"mode": "service-account", "secret": "svc-json"},
        },
    )
    cache = FakeClients(FakeFirewalls(), FakeInstances())
    platform._clients = cache  # type: ignore[assignment]

    install_at_run_process(monkeypatch, guest)
    # The returned legacy transport is only constructed here.
    monkeypatch.setattr("agentworks.plugins.gcp.platform.SSHTransport", lambda **kwargs: SimpleNamespace(**kwargs))
    return platform, cache
