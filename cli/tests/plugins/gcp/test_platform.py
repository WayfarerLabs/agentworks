"""Offline full-create, lifecycle, and transient-route behavior for GCE."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import compute_v1

from agentworks.capabilities.vm_platform import ssh_exposure
from agentworks.capabilities.vm_platform.tailscale_join import JOIN_FRAGMENT
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
from tests._gcp_platform_fakes import (
    PROJECT,
    SERVICE_SENTINEL,
    TAILSCALE_SENTINEL,
    ZONE,
    RecordingProgress,
    api_error,
    gce_ctx,
    gce_guest,
    gce_platform,
    gce_request,
)


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
    guest = gce_guest()
    platform, cache = gce_platform(monkeypatch, guest)
    progress = MagicMock()
    result = platform.create(gce_request(progress), gce_ctx())

    assert cache.secret_was_delivered
    [request] = cache.instances.insert_requests
    body = compute_v1.Instance.to_dict(request.instance_resource)
    serialized = repr(body)
    assert TAILSCALE_SENTINEL not in serialized
    assert SERVICE_SENTINEL not in serialized
    assert body["service_accounts"] == []
    assert body["disks"][0]["auto_delete"] is True
    assert body["network_interfaces"][0]["stack_type"] == "IPV4_ONLY"
    metadata = {item["key"]: item["value"] for item in body["metadata"]["items"]}
    assert TAILSCALE_SENTINEL not in metadata["startup-script"]
    assert request.request_id
    assert [b"AGW_SECRET=" in script for script in guest.scripts] == [False, False, True, False]
    assert guest.payloads == ["true", GCE_READINESS_COMMAND, JOIN_FRAGMENT, "tailscale ip -4"]
    assert all(TAILSCALE_SENTINEL not in arg for argv in guest.argvs for arg in argv)
    assert result.tailnet_address == "100.64.0.9"
    assert "203.0.113.19" not in repr(result.platform_metadata)
    assert result.platform_metadata["allow_source_ranges"] == "198.18.0.7/32"
    assert TAILSCALE_SENTINEL not in repr(progress.mock_calls)
    assert SERVICE_SENTINEL not in repr(progress.mock_calls)


def test_combined_progress_precedes_readiness_and_fixed_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    platform, _cache = gce_platform(monkeypatch, gce_guest(events=events))

    platform.create(gce_request(RecordingProgress(events)), gce_ctx())

    readiness = events.index(f"root:{GCE_READINESS_COMMAND}")
    delivery = events.index(f"root:{JOIN_FRAGMENT}")
    steps = [index for index, event in enumerate(events) if event.startswith("progress:")]
    outputs = [index for index, event in enumerate(events) if event.startswith("output:")]
    # The last step is announced before the wait it names, and its outcome reported after the join.
    assert max(steps) < readiness < delivery < max(outputs)


def test_combined_progress_does_not_claim_completion_when_readiness_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    guest = gce_guest(
        failures={GCE_READINESS_COMMAND: [ConnectivityError("startup marker unavailable")]}, events=events
    )
    platform, _cache = gce_platform(monkeypatch, guest)

    with pytest.raises(ProvisioningError):
        platform.create(gce_request(RecordingProgress(events)), gce_ctx())

    readiness = events.index(f"root:{GCE_READINESS_COMMAND}")
    assert any(event.startswith("progress:") for event in events[:readiness])
    assert f"root:{JOIN_FRAGMENT}" not in events
    assert not any(event.startswith("output:") for event in events[readiness:])


def test_close_reconstructs_stable_allow_from_original_normalized_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx("203.0.113.9"))
    vm = _vm(result.platform_metadata)
    allow_name = result.platform_metadata["allow_rule"]
    assert result.platform_metadata["allow_source_ranges"] == "198.18.0.7/32,203.0.113.9/32"

    platform.close_provisioning_access(vm, gce_ctx("192.0.2.44/32"))

    assert allow_name not in cache.firewalls.resources


@pytest.mark.parametrize(
    "failure_stage",
    ("deny-insert", "allow-insert", "instance-insert", "readiness"),
)
def test_create_failures_clean_every_partial_resource_set(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    provider_failure = api_error(api_exceptions.ServiceUnavailable, "provider-private-detail")
    transport_failures: dict[str, list[BaseException]] = {}
    guest = gce_guest(failures=transport_failures)
    platform, cache = gce_platform(monkeypatch, guest)
    if failure_stage == "deny-insert":
        cache.firewalls.insert_failures = [provider_failure, provider_failure]
    elif failure_stage == "allow-insert":
        cache.firewalls.insert_failures = [None, provider_failure, provider_failure]
    elif failure_stage == "instance-insert":
        cache.instances.insert_failures = [provider_failure, provider_failure]
    else:
        transport_failures[GCE_READINESS_COMMAND] = [ConnectivityError("startup marker unavailable")]

    with pytest.raises((ConnectivityError, ProvisioningError)):
        platform.create(gce_request(), gce_ctx())

    assert cache.instances.resource is None
    assert cache.firewalls.resources == {}
    assert guest.joins == 0
    if failure_stage == "readiness":
        assert len(cache.instances.delete_requests) == 1


@pytest.mark.parametrize("interrupt_stage", ("deny-wait", "allow-wait", "instance-wait"))
def test_first_interrupt_carries_operation_ownership_through_platform_rollback(
    monkeypatch: pytest.MonkeyPatch,
    interrupt_stage: str,
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    if interrupt_stage == "deny-wait":
        cache.firewalls.insert_wait_failures = [KeyboardInterrupt(interrupt_stage)]
    elif interrupt_stage == "allow-wait":
        cache.firewalls.insert_wait_failures = [None, KeyboardInterrupt(interrupt_stage)]
    else:
        cache.instances.insert_wait_failures = [KeyboardInterrupt(interrupt_stage)]

    with pytest.raises(KeyboardInterrupt, match=interrupt_stage):
        platform.create(gce_request(), gce_ctx())

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
    guest = gce_guest(failures={JOIN_FRAGMENT: [ConnectivityError(SERVICE_SENTINEL)]})
    platform, cache = gce_platform(monkeypatch, guest)
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
        platform.create(gce_request(), gce_ctx())

    assert caught.value is first
    assert first.__cause__ is None
    assert first.__context__ is None
    assert SERVICE_SENTINEL not in repr(first)
    assert rollback_calls == 2
    assert cache.instances.resource is None
    assert cache.firewalls.resources == {}


def test_second_interrupt_abandons_platform_rollback_without_mutating_survivors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.cleanup.output.warn", warnings.append)
    platform, cache = gce_platform(
        monkeypatch, gce_guest(failures={GCE_READINESS_COMMAND: [KeyboardInterrupt("first")]})
    )
    cache.firewalls.delete_failures = [KeyboardInterrupt("second")]

    with pytest.raises(KeyboardInterrupt, match="first"):
        platform.create(gce_request(), gce_ctx())

    assert cache.instances.resource is not None
    assert len(cache.firewalls.resources) == 2
    assert cache.instances.delete_requests == []
    message = "\n".join(warnings)
    assert "firewall-rules delete" not in message


def test_lifecycle_is_idempotent_live_ip_and_transient_routes_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx())
    vm = _vm(result.platform_metadata)
    firewalls = cache.firewalls
    instances = cache.instances

    platform.close_provisioning_access(vm, gce_ctx())
    assert result.platform_metadata["allow_rule"] not in firewalls.resources
    instances.get_calls.clear()
    assert platform.status(vm, gce_ctx()) is VMStatus.RUNNING
    assert len(instances.get_calls) == 1
    assert instances.get_calls[0]["retry"] is None
    assert instances.get_calls[0]["timeout"] == 10
    platform.stop(vm, gce_ctx())
    platform.stop(vm, gce_ctx())
    assert len(instances.stop_requests) == 1
    assert platform.status(vm, gce_ctx()) is VMStatus.STOPPED
    platform.start(vm, gce_ctx())
    platform.start(vm, gce_ctx())
    assert len(instances.start_requests) == 1
    assert platform._public_address(vm, gce_ctx()) == "203.0.113.20"

    with platform._transient_route(vm, gce_ctx()):
        route_one = set(firewalls.resources) - {result.platform_metadata["deny_rule"]}
        assert len(route_one) == 1
        with platform._transient_route(vm, gce_ctx()):
            routes_both = set(firewalls.resources) - {result.platform_metadata["deny_rule"]}
            assert len(routes_both) == 2
            assert route_one < routes_both
        assert (set(firewalls.resources) - {result.platform_metadata["deny_rule"]}) == route_one
    assert set(firewalls.resources) == {result.platform_metadata["deny_rule"]}

    platform.delete(vm, gce_ctx())
    platform.delete(vm, gce_ctx())
    assert instances.resource is None
    assert firewalls.resources == {}
    assert len(instances.delete_requests) == 1


def test_the_public_address_read_fails_when_the_owned_instance_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx())
    vm = _vm(result.platform_metadata)
    cache.instances.resource = None

    with pytest.raises(StateError) as caught:
        platform._public_address(vm, gce_ctx())

    assert caught.value.entity_kind == "vm"
    assert caught.value.entity_name == vm.name


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
    platform, _cache = gce_platform(monkeypatch, gce_guest())
    vm = _vm(platform.create(gce_request(), gce_ctx()).platform_metadata)

    def fail_wait(*_args: object, **_kwargs: object) -> None:
        raise wait_failure

    monkeypatch.setattr("agentworks.plugins.gcp.errors.wait_for_extended_operation", fail_wait)

    with pytest.raises(type(wait_failure)) as caught:
        platform.stop(vm, gce_ctx())

    assert caught.value is wait_failure


def test_join_failure_rolls_back_and_exception_graph_is_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest(fail_join=True))
    with pytest.raises(ProvisioningError) as caught:
        platform.create(gce_request(), gce_ctx())
    assert cache.instances.resource is None
    assert cache.firewalls.resources == {}
    pending: list[BaseException | None] = [caught.value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        assert TAILSCALE_SENTINEL not in str(current)
        assert SERVICE_SENTINEL not in str(current)
        pending.extend((current.__cause__, current.__context__))


@pytest.mark.parametrize(
    "machine",
    [
        compute_v1.MachineType(
            name="e2-standard-2",
            self_link=f"projects/{PROJECT}/zones/{ZONE}/machineTypes/e2-standard-2",
            guest_cpus=2,
            memory_mb=8192,
            architecture="x86_64",
            maximum_persistent_disks=0,
            accelerators=[],
        ),
        compute_v1.MachineType(
            name="e2-standard-2",
            self_link=f"projects/{PROJECT}/zones/{ZONE}/machineTypes/e2-standard-2",
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
    platform, cache = gce_platform(monkeypatch, gce_guest())
    cache.clients["machine-types"] = SimpleNamespace(get=lambda **_kwargs: machine)

    with pytest.raises(ConfigError, match="e2-standard-2"):
        platform.create(gce_request(), gce_ctx())

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
    platform, cache = gce_platform(monkeypatch, gce_guest())
    cache.clients["machine-types"] = SimpleNamespace(get=lambda **_kwargs: machine)

    with pytest.raises(ConfigError, match=message):
        platform.create(gce_request(), gce_ctx())

    assert cache.firewalls.insert_requests == []
    assert cache.instances.insert_requests == []


def test_residual_definitive_insert_rejection_rolls_back_and_stays_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    provider_sentinel = "provider-private-insert-SENTINEL"
    provider = api_error(api_exceptions.ServiceUnavailable, provider_sentinel)
    provider.__cause__ = RuntimeError(provider_sentinel)
    cache.instances.insert_wait_failures = [provider]
    cache.instances.insert_operation_errors = [
        compute_v1.Error(errors=[compute_v1.Errors(code="UNKNOWN", message=provider_sentinel)])
    ]

    with pytest.raises(GCEOperationError) as caught:
        platform.create(gce_request(), gce_ctx())

    assert type(caught.value) is GCEOperationError
    assert "e2-standard-2" in str(caught.value)
    assert "IAM, quota, and request prerequisites first" in (caught.value.hint or "")
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
    platform, cache = gce_platform(monkeypatch, gce_guest())
    platform.runup(gce_ctx())
    assert cache.secret_was_delivered
    assert cache.firewalls.insert_requests == []
    assert cache.instances.insert_requests == []

    cache.instances.resource = compute_v1.Instance(id=999, name="vm-a")
    with pytest.raises(AlreadyExistsError):
        platform.create(gce_request(), gce_ctx())
    assert cache.firewalls.insert_requests == []
    assert cache.instances.insert_requests == []


def test_delete_retains_same_name_different_id_instance_and_lifetime_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx())
    vm = _vm(result.platform_metadata)
    assert cache.instances.resource is not None
    cache.instances.resource.id = 999

    with pytest.raises(GCEOperationError, match="not proven absent") as caught:
        platform.delete(vm, gce_ctx())
    assert cache.instances.resource is not None
    assert cache.instances.delete_requests == []
    assert result.platform_metadata["deny_rule"] in cache.firewalls.resources
    assert "do not delete by name" in (caught.value.hint or "")


def test_delete_retains_same_id_mutated_allow_but_still_deletes_instance_and_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.platform.output.warn", warnings.append)
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx())
    vm = _vm(result.platform_metadata)
    allow_name = result.platform_metadata["allow_rule"]
    deny_name = result.platform_metadata["deny_rule"]
    mutated = cache.firewalls.resources[allow_name]
    mutated.priority = 7

    platform.delete(vm, gce_ctx())

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
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx())
    metadata = dict(result.platform_metadata)
    metadata["allow_source_ranges"] = "198.18.0.7"

    with pytest.raises(StateError, match="invalid persisted GCE allow-source metadata"):
        platform.delete(_vm(metadata), gce_ctx())

    assert cache.instances.delete_requests == []
    assert cache.firewalls.delete_requests == []


@pytest.mark.parametrize("provider_error", (api_exceptions.ServiceUnavailable, api_exceptions.PermissionDenied))
def test_delete_continues_after_allow_read_failure_and_reports_retained_allow(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: type[Exception],
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.platform.output.warn", warnings.append)
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx())
    vm = _vm(result.platform_metadata)
    allow_name = result.platform_metadata["allow_rule"]
    deny_name = result.platform_metadata["deny_rule"]
    cache.firewalls.get_failures[allow_name] = [api_error(provider_error, "provider-private-detail")]

    platform.delete(vm, gce_ctx())

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
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx())
    vm = _vm(result.platform_metadata)
    allow_name = result.platform_metadata["allow_rule"]
    deny_name = result.platform_metadata["deny_rule"]
    unavailable = api_error(api_exceptions.ServiceUnavailable, "provider-private-detail")
    cache.firewalls.get_failures[allow_name] = [unavailable]
    cache.instances.delete_failures = [unavailable]

    with pytest.raises(GCEOperationError, match="not proven absent") as caught:
        platform.delete(vm, gce_ctx())

    assert len(cache.instances.delete_requests) == 1
    assert cache.instances.resource is not None
    assert {allow_name, deny_name} <= set(cache.firewalls.resources)
    assert all(request.firewall != deny_name for request in cache.firewalls.delete_requests)
    assert "Owned deny rule retained while instance absence is unproven" in (caught.value.hint or "")
    assert "provider-private-detail" not in (caught.value.hint or "")


def test_the_stable_allow_close_fails_closed_on_provider_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.plugins.gcp.platform.output.warn", warnings.append)
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx())
    vm = _vm(result.platform_metadata)
    allow_name = result.platform_metadata["allow_rule"]
    cache.firewalls.get_failures[allow_name] = [api_error(api_exceptions.ServiceUnavailable, "provider-private-detail")]

    platform.close_provisioning_access(vm, gce_ctx())

    assert allow_name in cache.firewalls.resources
    assert all(request.firewall != allow_name for request in cache.firewalls.delete_requests)
    assert "not proven absent" in "\n".join(warnings)
    assert "provider-private-detail" not in "\n".join(warnings)


def test_transient_route_open_failure_never_yields_or_mutates_stable_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx())
    vm = _vm(result.platform_metadata)
    platform.close_provisioning_access(vm, gce_ctx())
    unavailable_one = api_error(api_exceptions.ServiceUnavailable, "first-private-detail")
    unavailable_two = api_error(api_exceptions.ServiceUnavailable, "second-private-detail")
    cache.firewalls.insert_failures = [unavailable_one, unavailable_two]
    yielded = False

    with pytest.raises(ConnectivityError), platform._transient_route(vm, gce_ctx()):
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
    platform, cache = gce_platform(monkeypatch, gce_guest())
    result = platform.create(gce_request(), gce_ctx())
    vm = _vm(result.platform_metadata)
    platform.close_provisioning_access(vm, gce_ctx())
    cache.firewalls.delete_failures = [api_error(api_exceptions.ServiceUnavailable, "provider-private-detail")]

    with platform._transient_route(vm, gce_ctx()):
        [route_name] = set(cache.firewalls.resources) - {result.platform_metadata["deny_rule"]}

    assert {result.platform_metadata["deny_rule"], route_name} == set(cache.firewalls.resources)
    message = "\n".join(warnings)
    assert route_name in message
    assert "not proven absent" in message
    assert "provider-private-detail" not in message
