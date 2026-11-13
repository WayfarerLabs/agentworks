"""Owned GCE power recovery uses the native start or resume operation."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from google.cloud import compute_v1

from agentworks.errors import AlreadyExistsError, NotFoundError, StateError
from agentworks.plugins.gcp.errors import GCEIndeterminateOperationError, GCEOperationError
from tests._gcp_platform_fakes import FakeClients, FakeOperation, gce_ctx, gce_guest, gce_platform, gce_request
from tests.plugins.gcp.test_platform import _vm

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db import VMRow
    from agentworks.plugins.gcp.platform import GCEPlatform


@pytest.fixture
def power_vm(monkeypatch: pytest.MonkeyPatch) -> tuple[GCEPlatform, FakeClients, VMRow]:
    monkeypatch.setattr("agentworks.capabilities.vm_platform.ssh_exposure._egress_ip_cache", None)
    monkeypatch.setattr("agentworks.capabilities.vm_platform.ssh_exposure.detect_egress_ip", lambda: "198.18.0.7")
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx())
    assert cache.instances.resource is not None
    cache.instances.resource.status = "SUSPENDED"
    cache.instances.get_calls.clear()
    return platform, cache, _vm(result.platform_metadata)


def _capture_resume(
    monkeypatch: pytest.MonkeyPatch,
    cache: FakeClients,
    change: Callable[[FakeOperation], None],
) -> list[FakeOperation]:
    operations: list[FakeOperation] = []
    resume = cache.instances.resume

    def capture(*, request: compute_v1.ResumeInstanceRequest, **kwargs: object) -> FakeOperation:
        assert kwargs == {"retry": None}
        operation = resume(request=request, **kwargs)
        operations.append(operation)
        change(operation)
        return operation

    monkeypatch.setattr(cache.instances, "resume", capture)
    return operations


def _no_cleanup(cache: FakeClients) -> None:
    assert not cache.instances.start_requests
    assert not cache.instances.stop_requests
    assert not cache.instances.delete_requests


@pytest.mark.parametrize(
    "status,action", [("SUSPENDED", "resume"), ("TERMINATED", "start"), ("RUNNING", None), ("PROVISIONING", "start")]
)
def test_native_power_dispatch_preserves_vm(
    power_vm: tuple[GCEPlatform, FakeClients, VMRow], status: str, action: str | None
) -> None:
    platform, cache, vm = power_vm
    assert cache.instances.resource is not None
    cache.instances.resource.status = status
    before = deepcopy(vm)

    platform.start(vm, gce_ctx())

    assert len(cache.instances.resume_requests) == int(action == "resume")
    assert len(cache.instances.start_requests) == int(action == "start")
    assert not cache.instances.stop_requests
    assert not cache.instances.delete_requests
    assert vm == before
    assert cache.instances.resource.id == int(vm.platform_metadata["instance_id"])


def test_resume_coordinates_fresh_request_ids_and_verified_operation_wait(
    monkeypatch: pytest.MonkeyPatch, power_vm: tuple[GCEPlatform, FakeClients, VMRow]
) -> None:
    platform, cache, vm = power_vm
    operations = _capture_resume(monkeypatch, cache, lambda operation: None)
    for _ in range(2):
        assert cache.instances.resource is not None
        cache.instances.resource.status = "SUSPENDED"
        platform.start(vm, gce_ctx())

    requests = cache.instances.resume_requests
    assert len(requests) == 2
    assert requests[0].request_id != requests[1].request_id
    for request, operation in zip(requests, operations, strict=True):
        assert (request.project, request.zone, request.instance) == ("project-a", "us-central1-a", "vm-a")
        assert UUID(request.request_id).version == 4
        assert operation.client_operation_id == request.request_id
        assert operation.waits == [300.0]
    _no_cleanup(cache)


@pytest.mark.parametrize("transient", ["STOPPING", "SUSPENDING"])
@pytest.mark.parametrize("settled,action", [("SUSPENDED", "resume"), ("TERMINATED", "start"), ("RUNNING", None)])
def test_power_transition_settles_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    power_vm: tuple[GCEPlatform, FakeClients, VMRow],
    transient: str,
    settled: str,
    action: str | None,
) -> None:
    platform, cache, vm = power_vm
    original = cache.instances.resource
    assert original is not None
    original.status = transient
    ready = compute_v1.Instance(compute_v1.Instance.to_dict(original))
    ready.status = settled
    sleep_calls: list[float] = []
    elapsed = 0.0

    def finish_transition(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds
        assert not cache.instances.start_requests
        assert not cache.instances.resume_requests
        sleep_calls.append(seconds)
        cache.instances.resource = ready

    monkeypatch.setattr("agentworks.plugins.gcp.instance.time.sleep", finish_transition)
    monkeypatch.setattr("agentworks.plugins.gcp.instance.time.monotonic", lambda: elapsed)
    platform.start(vm, gce_ctx())

    assert sleep_calls == [2.0]
    assert original.status == transient
    assert len(cache.instances.resume_requests) == int(action == "resume")
    assert len(cache.instances.start_requests) == int(action == "start")
    assert len(cache.instances.get_calls) == (3 if action is None else 4)


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_operation_id", "unrelated-request"),
        ("operation_type", "start"),
        ("target_link", "projects/other/zones/us-central1-a/instances/vm-a"),
        ("target_link", "projects/project-a/zones/other/instances/vm-a"),
        ("target_link", "projects/project-a/zones/us-central1-a/instances/other"),
        ("target_id", 202),
        ("target_id", 0),
        ("target_id", None),
    ],
)
def test_resume_refuses_unowned_operation_before_wait(
    monkeypatch: pytest.MonkeyPatch,
    power_vm: tuple[GCEPlatform, FakeClients, VMRow],
    field: str,
    value: object,
) -> None:
    platform, cache, vm = power_vm
    operations = _capture_resume(monkeypatch, cache, lambda operation: setattr(operation, field, value))

    with pytest.raises(GCEOperationError):
        platform.start(vm, gce_ctx())

    assert len(operations) == 1
    assert operations[0].waits == []
    assert len(cache.instances.get_calls) == 1
    _no_cleanup(cache)


@pytest.mark.parametrize("stage", ["before", "settling", "after-resume"])
@pytest.mark.parametrize("replacement", [False, True])
def test_every_power_read_rejects_missing_or_replaced_incarnation(
    monkeypatch: pytest.MonkeyPatch,
    power_vm: tuple[GCEPlatform, FakeClients, VMRow],
    stage: str,
    replacement: bool,
) -> None:
    platform, cache, vm = power_vm
    assert cache.instances.resource is not None
    cache.instances.resource.status = "SUSPENDING" if stage == "settling" else "SUSPENDED"
    get = cache.instances.get

    def change_incarnation(**kwargs: object) -> compute_v1.Instance:
        change_after_reads = 2 if stage == "settling" else 1
        if stage == "before" or len(cache.instances.get_calls) >= change_after_reads:
            if replacement:
                assert cache.instances.resource is not None
                cache.instances.resource.id = 202
            else:
                cache.instances.resource = None
        return get(**kwargs)

    monkeypatch.setattr(cache.instances, "get", change_incarnation)
    monkeypatch.setattr("agentworks.plugins.gcp.instance.time.sleep", lambda seconds: None)
    error = AlreadyExistsError if replacement else StateError if stage == "before" else NotFoundError
    with pytest.raises(error):
        platform.start(vm, gce_ctx())

    assert len(cache.instances.resume_requests) == int(stage == "after-resume")
    _no_cleanup(cache)


@pytest.mark.parametrize("stage", ["settling", "after-resume"])
def test_power_poll_timeout_never_reverses_or_cleans_up(
    monkeypatch: pytest.MonkeyPatch, power_vm: tuple[GCEPlatform, FakeClients, VMRow], stage: str
) -> None:
    platform, cache, vm = power_vm
    assert cache.instances.resource is not None
    cache.instances.resource.status = "SUSPENDING" if stage == "settling" else "SUSPENDED"

    def stay_suspended(operation: FakeOperation) -> None:
        assert cache.instances.resource is not None
        cache.instances.resource.status = "SUSPENDED"

    _capture_resume(monkeypatch, cache, stay_suspended)
    ticks = iter((0.0, 301.0))
    monkeypatch.setattr("agentworks.plugins.gcp.instance.time.monotonic", lambda: next(ticks))
    with pytest.raises(GCEOperationError):
        platform.start(vm, gce_ctx())

    assert len(cache.instances.resume_requests) == int(stage == "after-resume")
    _no_cleanup(cache)


def test_resume_operation_timeout_is_indeterminate_without_cleanup(
    monkeypatch: pytest.MonkeyPatch, power_vm: tuple[GCEPlatform, FakeClients, VMRow]
) -> None:
    platform, cache, vm = power_vm
    operations = _capture_resume(monkeypatch, cache, lambda operation: setattr(operation, "failure", TimeoutError()))
    with pytest.raises(GCEIndeterminateOperationError):
        platform.start(vm, gce_ctx())

    assert operations[0].waits == [300.0]
    assert len(cache.instances.get_calls) == 1
    _no_cleanup(cache)


@pytest.mark.parametrize("stage", ["request", "operation-wait", "settling", "after-resume"])
def test_power_interrupt_propagates_exactly_without_opposite_action(
    monkeypatch: pytest.MonkeyPatch, power_vm: tuple[GCEPlatform, FakeClients, VMRow], stage: str
) -> None:
    platform, cache, vm = power_vm
    interrupt = KeyboardInterrupt()
    before = deepcopy(vm)
    assert cache.instances.resource is not None
    cache.instances.resource.status = "SUSPENDING" if stage == "settling" else "SUSPENDED"

    def interrupt_request(operation: FakeOperation) -> None:
        if stage == "request":
            raise interrupt
        if stage == "operation-wait":
            operation.failure = interrupt

    _capture_resume(monkeypatch, cache, interrupt_request)
    get = cache.instances.get

    def interrupt_poll(**kwargs: object) -> compute_v1.Instance:
        if stage in {"settling", "after-resume"} and cache.instances.get_calls:
            raise interrupt
        return get(**kwargs)

    monkeypatch.setattr(cache.instances, "get", interrupt_poll)
    with pytest.raises(KeyboardInterrupt) as raised:
        platform.start(vm, gce_ctx())

    assert raised.value is interrupt
    assert vm == before
    assert len(cache.instances.resume_requests) == int(stage != "settling")
    _no_cleanup(cache)
