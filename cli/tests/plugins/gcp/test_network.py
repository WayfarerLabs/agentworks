"""Default/subnet network resolution and classic firewall safety helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import compute_v1

from agentworks.capabilities.base import RunContext
from agentworks.errors import ConfigError
from agentworks.plugins.gcp.config import GcpGCEConfig
from agentworks.plugins.gcp.errors import GCEConflictError, GCEOperationError
from agentworks.plugins.gcp.network import (
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

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[float] = []

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


def _owned_rule(name: str, *, priority: int, deny: bool) -> compute_v1.Firewall:
    return _rule(
        name,
        priority=priority,
        targets=("vm-tag",),
        source_ranges=("0.0.0.0/0",) if deny else ("203.0.113.7/32",),
        denied=(compute_v1.Denied(I_p_protocol="all"),) if deny else (),
        allowed=() if deny else (compute_v1.Allowed(I_p_protocol="tcp", ports=["22"]),),
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
    ) -> None:
        self.states = states
        self.operation = operation or _Operation()
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
        return self.operation

    def delete(self, **kwargs: object) -> _Operation:
        self.calls.append(("delete", kwargs))
        return self.operation


def test_firewall_name_collision_and_absence() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    present = _FirewallClient(states=iter([expected]))
    with pytest.raises(GCEConflictError, match="already exists"):
        require_firewall_name_available(present, project_id="project-a", rule_name="allow")
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
    assert reconcile_firewall(client, project_id="project-a", expected=expected) is expected_state


def test_indeterminate_insert_reconciles_realized_rule_as_success() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    operation = _Operation(TimeoutError("provider timeout"))
    client = _FirewallClient(states=iter([expected]), operation=operation)
    assert (
        insert_firewall_reconciled(client, project_id="project-a", firewall=expected, timeout=17)
        is FirewallState.REALIZED
    )
    assert operation.calls == [17]


def test_indeterminate_insert_absence_preserves_sanitized_timeout() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    client = _FirewallClient(states=iter([None]), operation=_Operation(TimeoutError("provider timeout")))
    with pytest.raises(GCEOperationError) as caught:
        insert_firewall_reconciled(client, project_id="project-a", firewall=expected, timeout=17)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_indeterminate_insert_mismatch_is_collision_and_never_deleted() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    mismatched = _owned_rule("allow", priority=1, deny=False)
    client = _FirewallClient(states=iter([mismatched]), operation=_Operation(TimeoutError("provider timeout")))
    with pytest.raises(GCEConflictError, match="different shape"):
        insert_firewall_reconciled(client, project_id="project-a", firewall=expected, timeout=17)
    assert [name for name, _kwargs in client.calls] == ["insert", "get"]


def test_matching_firewall_delete_proves_absence_and_mismatch_is_retained() -> None:
    expected = _owned_rule("allow", priority=0, deny=False)
    client = _FirewallClient(states=iter([expected, None]))
    assert (
        delete_matching_firewall(client, project_id="project-a", expected=expected, timeout=11) is FirewallState.ABSENT
    )
    assert [name for name, _kwargs in client.calls] == ["get", "delete", "get"]

    mismatched = _owned_rule("allow", priority=1, deny=False)
    collision = _FirewallClient(states=iter([mismatched]))
    assert (
        delete_matching_firewall(collision, project_id="project-a", expected=expected, timeout=11)
        is FirewallState.MISMATCHED
    )
    assert [name for name, _kwargs in collision.calls] == ["get"]
