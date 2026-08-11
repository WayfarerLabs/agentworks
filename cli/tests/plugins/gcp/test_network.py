"""Default/subnet network resolution and classic firewall safety helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import compute_v1

from agentworks.capabilities.base import RunContext
from agentworks.errors import AlreadyExistsError, AuthorizationError, ConfigError
from agentworks.plugins.gcp.config import GcpGCEConfig
from agentworks.plugins.gcp.errors import GCECapacityError, GCEIndeterminateOperationError, GCEOperationError
from agentworks.plugins.gcp.network import (
    FirewallInsertAttempt,
    FirewallOwnership,
    FirewallShape,
    FirewallState,
    delete_matching_firewall,
    get_network,
    insert_firewall_reconciled,
    reconcile_firewall,
    reject_priority_zero_conflicts,
    require_classic_first,
    require_firewall_name_available,
    resolve_network,
    zone_region,
)

_NETWORK = "https://www.googleapis.com/compute/v1/projects/project-a/global/networks/default"
_REQUEST_ID = "2c12fd6f-97df-42e2-b394-fafb4431020b"


def _api_error(kind: type[Exception], message: str) -> Exception:
    return cast("Callable[[str], Exception]", kind)(message)


class _Cache:
    def __init__(self, **clients: object) -> None:
        self.clients = clients

    def client(self, kind: str, _ctx: RunContext) -> Any:
        return self.clients[kind]


class _GetClient:
    def __init__(self, value: object = None, failure: Exception | None = None) -> None:
        self.value = value
        self.failure = failure
        self.calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.value


class _Operation:
    error_code = None

    def __init__(
        self,
        failure: BaseException | None = None,
        *,
        request_id: str = _REQUEST_ID,
        target_id: int = 101,
        target_link: str = "projects/project-a/global/firewalls/allow",
        operation_type: str = "insert",
        status: object = None,
        error: object = None,
    ) -> None:
        self.failure = failure
        self.calls: list[float] = []
        self.client_operation_id = request_id
        self.target_id = target_id
        self.target_link = target_link
        self.operation_type = operation_type
        self.status = status
        self.error = error

    def result(self, *, timeout: float) -> None:
        self.calls.append(timeout)
        if self.failure is not None:
            raise self.failure


def _config(*, subnet: str | None = None) -> GcpGCEConfig:
    return GcpGCEConfig(
        name="gcp-gce",
        project_id="project-a",
        zone="us-central1-a",
        subnet=subnet,
    )


def _rule(
    name: str,
    *,
    resource_id: int = 0,
    priority: int = 0,
    direction: str = "INGRESS",
    disabled: bool = False,
    targets: tuple[str, ...] = (),
    source_ranges: tuple[str, ...] = ("0.0.0.0/0",),
    allowed: tuple[compute_v1.Allowed, ...] = (),
    denied: tuple[compute_v1.Denied, ...] = (),
    network: str = _NETWORK,
) -> compute_v1.Firewall:
    return compute_v1.Firewall(
        id=resource_id,
        name=name,
        network=network,
        direction=direction,
        disabled=disabled,
        priority=priority,
        target_tags=targets,
        source_ranges=source_ranges,
        allowed=allowed,
        denied=denied,
    )


def _owned_rule(
    name: str,
    *,
    priority: int,
    deny: bool,
    resource_id: int = 101,
) -> compute_v1.Firewall:
    return _rule(
        name,
        priority=priority,
        targets=("vm-tag",),
        source_ranges=("0.0.0.0/0",) if deny else ("203.0.113.7/32",),
        denied=(compute_v1.Denied(I_p_protocol="all"),) if deny else (),
        allowed=() if deny else (compute_v1.Allowed(I_p_protocol="tcp", ports=["22"]),),
        resource_id=resource_id,
    )


def test_zone_to_region_is_exact_and_invalid_shape_rejected() -> None:
    assert zone_region("us-central1-a") == "us-central1"
    assert zone_region("northamerica-northeast2-b") == "northamerica-northeast2"
    with pytest.raises(ConfigError, match="cannot identify"):
        zone_region("global")


def test_configured_subnet_resolves_in_zone_region_and_retains_network_url() -> None:
    subnet = compute_v1.Subnetwork(
        name="app-subnet",
        self_link="projects/project-a/regions/us-central1/subnetworks/app-subnet",
        network=_NETWORK,
    )
    client = _GetClient(subnet)
    selected = resolve_network(
        _Cache(subnetworks=client),  # type: ignore[arg-type]
        RunContext(),
        _config(subnet="app-subnet"),
    )
    assert selected.region == "us-central1"
    assert selected.network_url == _NETWORK
    assert selected.subnet_url == subnet.self_link
    assert client.calls == [{"project": "project-a", "region": "us-central1", "subnetwork": "app-subnet"}]


def test_omitted_subnet_requires_and_resolves_default_network() -> None:
    network = compute_v1.Network(name="default", self_link=_NETWORK)
    client = _GetClient(network)
    selected = resolve_network(_Cache(networks=client), RunContext(), _config())  # type: ignore[arg-type]
    assert selected.network_url == _NETWORK
    assert selected.subnet_url is None
    assert client.calls == [{"project": "project-a", "network": "default"}]

    missing = _GetClient(failure=_api_error(api_exceptions.NotFound, "gone"))
    with pytest.raises(ConfigError, match="has no default network"):
        resolve_network(_Cache(networks=missing), RunContext(), _config())  # type: ignore[arg-type]


def test_selected_network_is_re_read_and_identity_checked() -> None:
    network = compute_v1.Network(name="default", self_link=_NETWORK)
    client = _GetClient(network)
    assert (
        get_network(  # type: ignore[arg-type]
            _Cache(networks=client),
            RunContext(),
            project_id="project-a",
            network_url=_NETWORK,
        )
        is network
    )
    assert client.calls == [{"project": "project-a", "network": "default"}]

    wrong = _GetClient(compute_v1.Network(name="default", self_link="projects/project-a/global/networks/other"))
    with pytest.raises(ConfigError, match="unexpected resource identity"):
        get_network(  # type: ignore[arg-type]
            _Cache(networks=wrong),
            RunContext(),
            project_id="project-a",
            network_url=_NETWORK,
        )


def test_shared_vpc_host_project_subnet_is_explicitly_unsupported() -> None:
    subnet = compute_v1.Subnetwork(
        name="shared",
        self_link="projects/project-a/regions/us-central1/subnetworks/shared",
        network="projects/host-project/global/networks/shared",
    )
    with pytest.raises(ConfigError, match="shared VPC host project"):
        resolve_network(  # type: ignore[arg-type]
            _Cache(subnetworks=_GetClient(subnet)),
            RunContext(),
            _config(subnet="shared"),
        )


def test_network_policy_must_be_after_classic_firewall() -> None:
    require_classic_first(
        compute_v1.Network(name="default", network_firewall_policy_enforcement_order="AFTER_CLASSIC_FIREWALL"),
        project_id="project-a",
    )
    with pytest.raises(ConfigError, match="BEFORE_CLASSIC_FIREWALL"):
        require_classic_first(
            compute_v1.Network(name="default", network_firewall_policy_enforcement_order="BEFORE_CLASSIC_FIREWALL"),
            project_id="project-a",
        )


@pytest.mark.parametrize(
    "rule",
    [
        _rule("universal", allowed=(compute_v1.Allowed(I_p_protocol="tcp", ports=["443"]),)),
        _rule("tagged", targets=("vm-tag",), allowed=(compute_v1.Allowed(I_p_protocol="udp"),)),
    ],
    ids=("universal", "derived-tag"),
)
def test_applicable_priority_zero_allow_is_rejected(rule: compute_v1.Firewall) -> None:
    with pytest.raises(ConfigError, match="priority-zero ingress allow"):
        reject_priority_zero_conflicts(
            [rule],
            network_url=_NETWORK,
            operator_prefixes=("203.0.113.7/32",),
            target_tag="vm-tag",
        )


def test_runup_without_tag_only_rejects_universal_target() -> None:
    tagged = _rule("tagged", targets=("vm-tag",), allowed=(compute_v1.Allowed(I_p_protocol="tcp"),))
    reject_priority_zero_conflicts(
        [tagged],
        network_url=_NETWORK,
        operator_prefixes=("203.0.113.7/32",),
        target_tag=None,
    )


@pytest.mark.parametrize(
    "rule",
    [
        _rule("all", denied=(compute_v1.Denied(I_p_protocol="all"),)),
        _rule("tcp-all", denied=(compute_v1.Denied(I_p_protocol="tcp"),)),
        _rule("tcp-range", denied=(compute_v1.Denied(I_p_protocol="tcp", ports=["20-30"]),)),
        _rule("numeric-tcp", denied=(compute_v1.Denied(I_p_protocol="6", ports=["22"]),)),
        _rule(
            "source-overlap",
            source_ranges=("203.0.113.0/24",),
            denied=(compute_v1.Denied(I_p_protocol="tcp", ports=["22"]),),
        ),
    ],
    ids=("all", "tcp-all", "port-range", "numeric-tcp", "source-overlap"),
)
def test_priority_zero_deny_overlapping_operator_ssh_is_rejected(rule: compute_v1.Firewall) -> None:
    with pytest.raises(ConfigError, match="operator-SSH deny"):
        reject_priority_zero_conflicts(
            [rule],
            network_url=_NETWORK,
            operator_prefixes=("203.0.113.7/32",),
            target_tag="vm-tag",
        )


@pytest.mark.parametrize(
    "rule",
    [
        _rule("p1", priority=1, allowed=(compute_v1.Allowed(I_p_protocol="tcp"),)),
        _rule("disabled", disabled=True, allowed=(compute_v1.Allowed(I_p_protocol="tcp"),)),
        _rule("egress", direction="EGRESS", allowed=(compute_v1.Allowed(I_p_protocol="tcp"),)),
        _rule("other-tag", targets=("other",), allowed=(compute_v1.Allowed(I_p_protocol="tcp"),)),
        _rule("other-port", denied=(compute_v1.Denied(I_p_protocol="tcp", ports=["23"]),)),
        _rule("udp", denied=(compute_v1.Denied(I_p_protocol="udp", ports=["22"]),)),
        _rule(
            "other-source",
            source_ranges=("198.51.100.0/24",),
            denied=(compute_v1.Denied(I_p_protocol="tcp", ports=["22"]),),
        ),
        _rule(
            "other-network",
            network="projects/project-a/global/networks/other",
            allowed=(compute_v1.Allowed(I_p_protocol="tcp"),),
        ),
    ],
)
def test_non_conflicting_firewall_rules_are_accepted(rule: compute_v1.Firewall) -> None:
    reject_priority_zero_conflicts(
        [rule],
        network_url=_NETWORK,
        operator_prefixes=("203.0.113.7/32",),
        target_tag="vm-tag",
    )


def test_firewall_shape_canonicalizes_order_but_detects_behavior_changes() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    reordered = _owned_rule("allow", priority=0, deny=False)
    reordered.source_ranges = list(reversed(reordered.source_ranges))
    assert FirewallShape.from_resource(reordered) == FirewallShape.from_resource(expected)

    changed = _owned_rule("allow", priority=1, deny=False)
    assert FirewallShape.from_resource(changed) != FirewallShape.from_resource(expected)
    changed = _owned_rule("allow", priority=0, deny=False)
    changed.allowed[0].ports = ["22", "443"]
    assert FirewallShape.from_resource(changed) != FirewallShape.from_resource(expected)


class _FirewallClient:
    def __init__(
        self,
        *,
        states: Iterator[compute_v1.Firewall | Exception | None],
        operation: _Operation | None = None,
        insert_results: Iterator[_Operation | Exception] | None = None,
    ) -> None:
        self.states = states
        self.operation = operation or _Operation()
        self.insert_results = insert_results
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, **kwargs: object) -> compute_v1.Firewall:
        self.calls.append(("get", kwargs))
        state = next(self.states)
        if isinstance(state, Exception):
            raise state
        if state is None:
            raise _api_error(api_exceptions.NotFound, "gone")
        return state

    def insert(self, **kwargs: object) -> _Operation:
        self.calls.append(("insert", kwargs))
        result = next(self.insert_results) if self.insert_results is not None else self.operation
        if isinstance(result, Exception):
            raise result
        return result

    def delete(self, **kwargs: object) -> _Operation:
        self.calls.append(("delete", kwargs))
        request = cast("compute_v1.DeleteFirewallRequest", kwargs["request"])
        self.operation.client_operation_id = request.request_id
        self.operation.operation_type = "delete"
        self.operation.target_link = f"projects/{request.project}/global/firewalls/{request.firewall}"
        return self.operation


def test_firewall_name_collision_and_absence() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    present = _FirewallClient(states=iter([expected]))
    with pytest.raises(AlreadyExistsError, match="already exists") as caught:
        require_firewall_name_available(present, project_id="project-a", rule_name="allow")
    assert "inspect the existing provider identity" in (caught.value.hint or "")
    assert "Do not delete the rule by name" in (caught.value.hint or "")
    assert "remove" not in (caught.value.hint or "").lower()
    absent = _FirewallClient(states=iter([None]))
    require_firewall_name_available(absent, project_id="project-a", rule_name="allow")


@pytest.mark.parametrize(
    ("observed", "expected_state"),
    [
        (None, FirewallState.ABSENT),
        (_owned_rule("allow", priority=0, deny=False), FirewallState.REALIZED),
        (_owned_rule("allow", priority=1, deny=False), FirewallState.MISMATCHED),
    ],
    ids=("absent", "realized", "mismatched"),
)
def test_exact_firewall_reconciliation(
    observed: compute_v1.Firewall | None,
    expected_state: FirewallState,
) -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    client = _FirewallClient(states=iter([observed]))
    assert (
        reconcile_firewall(
            client,
            project_id="project-a",
            expected=expected,
            ownership=FirewallOwnership("allow", "101"),
        )
        is expected_state
    )


def test_indeterminate_operation_reconciles_only_matching_resource_id_as_success() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    operation = _Operation(TimeoutError("provider timeout"))
    client = _FirewallClient(states=iter([expected]), operation=operation)
    attempt = FirewallInsertAttempt("allow", _REQUEST_ID)
    assert insert_firewall_reconciled(
        client,
        project_id="project-a",
        zone="us-central1-a",
        firewall=expected,
        attempt=attempt,
        timeout=17,
    ) == FirewallOwnership("allow", "101")
    assert attempt.ownership == FirewallOwnership("allow", "101")
    request = client.calls[0][1]["request"]
    assert isinstance(request, compute_v1.InsertFirewallRequest)
    assert request.project == "project-a"
    assert request.request_id == _REQUEST_ID
    assert request.firewall_resource == expected
    assert client.calls[0][1]["retry"] is None
    assert operation.calls == [17]


def test_wait_interrupt_leaves_operation_ownership_for_safe_rollback() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    operation = _Operation(KeyboardInterrupt("stop"))
    client = _FirewallClient(states=iter([]), operation=operation)
    attempt = FirewallInsertAttempt("allow", _REQUEST_ID)

    with pytest.raises(KeyboardInterrupt, match="stop"):
        insert_firewall_reconciled(
            client,
            project_id="project-a",
            zone="us-central1-a",
            firewall=expected,
            attempt=attempt,
            timeout=17,
        )

    assert attempt.ownership == FirewallOwnership("allow", "101")
    assert operation.calls == [17]
    assert [name for name, _kwargs in client.calls] == ["insert"]

    rollback = _FirewallClient(states=iter([expected, None]))
    result = delete_matching_firewall(
        rollback,
        project_id="project-a",
        zone="us-central1-a",
        expected=expected,
        ownership=attempt.ownership,
        timeout=11,
    )
    assert result.state is FirewallState.ABSENT
    assert [name for name, _kwargs in rollback.calls] == ["get", "delete", "get"]


def test_pre_response_timeout_retries_once_with_same_request_id() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    operation = _Operation()
    client = _FirewallClient(
        states=iter([expected]),
        insert_results=iter([TimeoutError("lost response"), operation]),
    )
    attempt = FirewallInsertAttempt("allow", _REQUEST_ID)
    assert insert_firewall_reconciled(
        client,
        project_id="project-a",
        zone="us-central1-a",
        firewall=expected,
        attempt=attempt,
        timeout=17,
    ) == FirewallOwnership("allow", "101")
    insert_calls = [kwargs for name, kwargs in client.calls if name == "insert"]
    assert len(insert_calls) == 2
    assert insert_calls[0]["request"] is insert_calls[1]["request"]


def test_indeterminate_insert_absence_preserves_sanitized_timeout() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    client = _FirewallClient(states=iter([None]), operation=_Operation(TimeoutError("provider timeout")))
    with pytest.raises(GCEIndeterminateOperationError) as caught:
        insert_firewall_reconciled(
            client,
            project_id="project-a",
            zone="us-central1-a",
            firewall=expected,
            attempt=FirewallInsertAttempt("allow", _REQUEST_ID),
            timeout=17,
        )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_done_generic_failure_never_reconciles_matching_firewall_to_success() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    operation = _Operation(
        _api_error(api_exceptions.ServiceUnavailable, "provider-private-detail"),
        status=compute_v1.Operation.Status.DONE,
        error=compute_v1.Error(errors=[compute_v1.Errors(code="UNKNOWN_CODE")]),
    )
    client = _FirewallClient(states=iter([expected]), operation=operation)

    with pytest.raises(GCEOperationError) as caught:
        insert_firewall_reconciled(
            client,
            project_id="project-a",
            zone="us-central1-a",
            firewall=expected,
            attempt=FirewallInsertAttempt("allow", _REQUEST_ID),
            timeout=17,
        )

    assert type(caught.value) is GCEOperationError
    assert [name for name, _kwargs in client.calls] == ["insert"]


def test_done_capacity_firewall_failure_does_not_attribute_the_vm_zone() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    operation = _Operation(
        _api_error(api_exceptions.ServiceUnavailable, "provider-private-detail"),
        status=compute_v1.Operation.Status.DONE,
        error=compute_v1.Error(errors=[compute_v1.Errors(code="ZONE_RESOURCE_POOL_EXHAUSTED")]),
    )
    client = _FirewallClient(states=iter([expected]), operation=operation)

    with pytest.raises(GCECapacityError) as caught:
        insert_firewall_reconciled(
            client,
            project_id="project-a",
            zone="us-central1-a",
            firewall=expected,
            attempt=FirewallInsertAttempt("allow", _REQUEST_ID),
            timeout=17,
        )

    assert "us-central1-a" not in f"{caught.value} {caught.value.hint}"
    assert caught.value.hint == "retry later"
    assert [name for name, _kwargs in client.calls] == ["insert"]


@pytest.mark.parametrize(
    "observed",
    [
        _owned_rule("allow", priority=1, deny=False),
        _owned_rule("allow", priority=0, deny=False, resource_id=202),
    ],
    ids=("different-shape", "same-shape-different-resource-id"),
)
def test_indeterminate_insert_mismatch_is_collision_and_never_deleted(observed: compute_v1.Firewall) -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    client = _FirewallClient(states=iter([observed]), operation=_Operation(TimeoutError("provider timeout")))
    with pytest.raises(AlreadyExistsError, match="provider identity or shape"):
        insert_firewall_reconciled(
            client,
            project_id="project-a",
            zone="us-central1-a",
            firewall=expected,
            attempt=FirewallInsertAttempt("allow", _REQUEST_ID),
            timeout=17,
        )
    assert [name for name, _kwargs in client.calls] == ["insert", "get"]


def test_definite_already_exists_is_collision_without_shape_reconciliation() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    client = _FirewallClient(
        states=iter([]),
        insert_results=iter([_api_error(api_exceptions.AlreadyExists, "concurrent winner")]),
    )
    with pytest.raises(AlreadyExistsError, match="already exists"):
        insert_firewall_reconciled(
            client,
            project_id="project-a",
            zone="us-central1-a",
            firewall=expected,
            attempt=FirewallInsertAttempt("allow", _REQUEST_ID),
            timeout=17,
        )
    assert [name for name, _kwargs in client.calls] == ["insert"]


def test_same_request_retry_already_exists_remains_collision() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    client = _FirewallClient(
        states=iter([]),
        insert_results=iter(
            [
                TimeoutError("lost response"),
                _api_error(api_exceptions.AlreadyExists, "concurrent winner"),
            ]
        ),
    )
    with pytest.raises(AlreadyExistsError, match="already exists"):
        insert_firewall_reconciled(
            client,
            project_id="project-a",
            zone="us-central1-a",
            firewall=expected,
            attempt=FirewallInsertAttempt("allow", _REQUEST_ID),
            timeout=17,
        )
    assert [name for name, _kwargs in client.calls] == ["insert", "insert"]


def test_definite_permission_failure_is_not_retried_or_shape_reconciled() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    client = _FirewallClient(
        states=iter([]),
        insert_results=iter([_api_error(api_exceptions.PermissionDenied, "denied")]),
    )
    with pytest.raises(AuthorizationError):
        insert_firewall_reconciled(
            client,
            project_id="project-a",
            zone="us-central1-a",
            firewall=expected,
            attempt=FirewallInsertAttempt("allow", _REQUEST_ID),
            timeout=17,
        )
    assert [name for name, _kwargs in client.calls] == ["insert"]


@pytest.mark.parametrize(
    "operation",
    [
        _Operation(request_id="wrong"),
        _Operation(target_id=0),
        _Operation(target_link="projects/project-a/global/firewalls/other"),
        _Operation(operation_type="patch"),
    ],
    ids=("request-id", "target-id", "target-link", "operation-type"),
)
def test_incomplete_or_wrong_operation_identity_is_never_owned(operation: _Operation) -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    client = _FirewallClient(states=iter([]), operation=operation)
    attempt = FirewallInsertAttempt("allow", _REQUEST_ID)
    with pytest.raises(GCEOperationError, match="incomplete ownership identity"):
        insert_firewall_reconciled(
            client,
            project_id="project-a",
            zone="us-central1-a",
            firewall=expected,
            attempt=attempt,
            timeout=17,
        )
    assert attempt.ownership is None
    assert operation.calls == []
    assert [name for name, _kwargs in client.calls] == ["insert"]


def test_matching_firewall_delete_proves_absence_and_mismatch_is_retained() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    client = _FirewallClient(states=iter([expected, None]))
    result = delete_matching_firewall(
        client,
        project_id="project-a",
        zone="us-central1-a",
        expected=expected,
        ownership=FirewallOwnership("allow", "101"),
        timeout=11,
    )
    assert result.state is FirewallState.ABSENT
    assert result.observed_resource_id is None
    assert [name for name, _kwargs in client.calls] == ["get", "delete", "get"]

    mismatched = _owned_rule("allow", priority=1, deny=False)
    collision = _FirewallClient(states=iter([mismatched]))
    result = delete_matching_firewall(
        collision,
        project_id="project-a",
        zone="us-central1-a",
        expected=expected,
        ownership=FirewallOwnership("allow", "101"),
        timeout=11,
    )
    assert result.state is FirewallState.MISMATCHED
    assert result.observed_resource_id == "101"
    assert [name for name, _kwargs in collision.calls] == ["get"]


def test_definitive_firewall_delete_wait_failure_still_accepts_verified_absence() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    operation = _Operation(
        _api_error(api_exceptions.ServiceUnavailable, "provider-private-detail"),
        operation_type="delete",
        status=compute_v1.Operation.Status.DONE,
        error=compute_v1.Error(errors=[compute_v1.Errors(code="UNKNOWN_CODE")]),
    )
    client = _FirewallClient(states=iter([expected, None]), operation=operation)

    result = delete_matching_firewall(
        client,
        project_id="project-a",
        zone="us-central1-a",
        expected=expected,
        ownership=FirewallOwnership("allow", "101"),
        timeout=11,
    )

    assert result.state is FirewallState.ABSENT
    assert operation.calls == [11]
    assert [name for name, _kwargs in client.calls] == ["get", "delete", "get"]


def test_same_name_and_shape_but_different_resource_id_is_never_deleted() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    concurrent_winner = _owned_rule("allow", priority=0, deny=False, resource_id=202)
    client = _FirewallClient(states=iter([concurrent_winner]))
    result = delete_matching_firewall(
        client,
        project_id="project-a",
        zone="us-central1-a",
        expected=expected,
        ownership=FirewallOwnership("allow", "101"),
        timeout=11,
    )
    assert result.state is FirewallState.MISMATCHED
    assert result.observed_resource_id == "202"
    assert [name for name, _kwargs in client.calls] == ["get"]


def test_rule_without_provider_ownership_is_indeterminate_and_never_deleted() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    client = _FirewallClient(states=iter([expected]))
    result = delete_matching_firewall(
        client,
        project_id="project-a",
        zone="us-central1-a",
        expected=expected,
        ownership=None,
        timeout=11,
    )
    assert result.state is FirewallState.INDETERMINATE
    assert result.observed_resource_id == "101"
    assert [name for name, _kwargs in client.calls] == ["get"]
