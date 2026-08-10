"""GCE network selection, firewall policy checks, and exact reconciliation."""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, NamedTuple

from agentworks.errors import (
    AgentworksError,
    AlreadyExistsError,
    AuthorizationError,
    ConfigError,
    ConnectivityError,
    NotFoundError,
    TokenRejectedError,
)
from agentworks.plugins.gcp.errors import (
    GCEError,
    GCEOperationError,
    GCEQuotaError,
    call_google,
    call_google_optional,
    wait_for_extended_operation,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agentworks.capabilities.base import RunContext
    from agentworks.plugins.gcp.auth import GcpClientCache
    from agentworks.plugins.gcp.config import GcpGCEConfig

CLASSIC_FIRST = "AFTER_CLASSIC_FIREWALL"


@dataclass(frozen=True)
class NetworkSelection:
    """The resolved shared network state for one site."""

    region: str
    network_url: str
    subnet_url: str | None


class ProtocolPorts(NamedTuple):
    """One canonical firewall protocol plus its complete port set."""

    protocol: str
    ports: tuple[str, ...]


@dataclass(frozen=True)
class FirewallShape:
    """Every firewall field that can change an owned rule's behavior."""

    network: str
    direction: str
    disabled: bool
    priority: int
    target_tags: tuple[str, ...]
    target_service_accounts: tuple[str, ...]
    source_ranges: tuple[str, ...]
    source_tags: tuple[str, ...]
    source_service_accounts: tuple[str, ...]
    destination_ranges: tuple[str, ...]
    allowed: tuple[ProtocolPorts, ...]
    denied: tuple[ProtocolPorts, ...]

    @classmethod
    def from_resource(cls, firewall: Any) -> FirewallShape:
        """Canonicalize a typed Compute ``Firewall`` for exact comparison."""
        return cls(
            network=_canonical_resource_url(str(firewall.network)),
            direction=str(firewall.direction or "INGRESS").upper(),
            disabled=bool(firewall.disabled),
            priority=int(firewall.priority),
            target_tags=tuple(sorted(str(value) for value in firewall.target_tags)),
            target_service_accounts=tuple(sorted(str(value) for value in firewall.target_service_accounts)),
            source_ranges=tuple(sorted(str(value) for value in firewall.source_ranges)),
            source_tags=tuple(sorted(str(value) for value in firewall.source_tags)),
            source_service_accounts=tuple(sorted(str(value) for value in firewall.source_service_accounts)),
            destination_ranges=tuple(sorted(str(value) for value in firewall.destination_ranges)),
            allowed=_protocols(firewall.allowed),
            denied=_protocols(firewall.denied),
        )


class FirewallState(Enum):
    """What an exact-name reconciliation can prove."""

    ABSENT = "absent"
    REALIZED = "realized"
    MISMATCHED = "mismatched"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class FirewallOwnership:
    """One server-assigned firewall incarnation safe to reconcile later."""

    rule_name: str
    resource_id: str


@dataclass
class FirewallInsertAttempt:
    """The unique request identity and learned resource identity for one insert."""

    rule_name: str
    request_id: str
    ownership: FirewallOwnership | None = None
    submitted: bool = False

    @classmethod
    def create(cls, rule_name: str) -> FirewallInsertAttempt:
        """Create a non-zero GCE request ID before the first mutation."""
        return cls(rule_name=rule_name, request_id=str(uuid.uuid4()))


def zone_region(zone: str) -> str:
    """Derive the Compute region name from a validated zonal name."""
    region, separator, suffix = zone.rpartition("-")
    if not separator or not region or not suffix:
        raise ConfigError(f"GCE zone '{zone}' cannot identify a region")
    return region


def resolve_network(clients: GcpClientCache, ctx: RunContext, config: GcpGCEConfig) -> NetworkSelection:
    """Resolve a configured subnet or the project's default network."""
    region = zone_region(config.zone)
    if config.subnet is not None:
        client = clients.client("subnetworks", ctx)
        subnet = call_google_optional(
            lambda: client.get(
                project=config.project_id,
                region=region,
                subnetwork=config.subnet,
            ),
            operation="reading the configured subnetwork",
            resource=f"subnetwork {config.project_id}/{region}/{config.subnet}",
        )
        if subnet is None:
            raise ConfigError(
                f"GCE subnetwork '{config.subnet}' does not exist in region '{region}'",
                hint="create the subnetwork or correct vm-site platform.subnet",
            )
        network_url = str(subnet.network)
        subnet_url = str(subnet.self_link)
        if not network_url or not subnet_url:
            raise GCEError(f"GCE subnetwork '{config.subnet}' returned incomplete network identity")
        canonical = _canonical_resource_url(network_url)
        if not canonical.startswith(f"projects/{config.project_id}/"):
            raise ConfigError(
                f"GCE subnetwork '{config.subnet}' belongs to a shared VPC host project, which gcp-gce does not support"
            )
        return NetworkSelection(region, network_url, subnet_url)

    client = clients.client("networks", ctx)
    network = call_google_optional(
        lambda: client.get(project=config.project_id, network="default"),
        operation="reading the default network",
        resource=f"network {config.project_id}/default",
    )
    if network is None:
        raise ConfigError(
            f"GCE project '{config.project_id}' has no default network",
            hint="name a subnet in vm-site platform.subnet or provision the project's default network",
        )
    network_url = str(network.self_link)
    if not network_url:
        raise GCEError(f"GCE default network in project '{config.project_id}' returned no self link")
    return NetworkSelection(region, network_url, None)


def get_network(
    clients: GcpClientCache,
    ctx: RunContext,
    *,
    project_id: str,
    network_url: str,
) -> Any:
    """Read the selected network from its resolved resource URL."""
    network_name = _canonical_resource_url(network_url).rsplit("/", 1)[-1]
    client = clients.client("networks", ctx)
    network = call_google_optional(
        lambda: client.get(project=project_id, network=network_name),
        operation="reading the selected network",
        resource=f"network {project_id}/{network_name}",
    )
    if network is None:
        raise ConfigError(
            f"GCE network '{network_name}' does not exist in project '{project_id}'",
            hint="correct the vm-site subnet or restore the selected network before retrying",
        )
    if _canonical_resource_url(str(network.self_link)) != _canonical_resource_url(network_url):
        raise ConfigError(f"GCE network '{network_name}' resolved to an unexpected resource identity")
    return network


def require_classic_first(network: Any, *, project_id: str) -> None:
    """Reject network firewall policies that evaluate before classic VPC rules."""
    order = str(network.network_firewall_policy_enforcement_order)
    if order != CLASSIC_FIRST:
        raise ConfigError(
            f"GCE network '{network.name}' in project '{project_id}' uses firewall enforcement order "
            f"'{order or 'unspecified'}', expected '{CLASSIC_FIRST}'",
            hint="set networkFirewallPolicyEnforcementOrder to AFTER_CLASSIC_FIREWALL before using gcp-gce",
        )


def list_firewalls(clients: GcpClientCache, ctx: RunContext, project_id: str) -> list[Any]:
    """Read every classic VPC firewall rule in the target project."""
    client = clients.client("firewalls", ctx)
    return call_google(
        lambda: list(client.list(project=project_id)),
        operation="listing classic VPC firewall rules",
        resource=f"project {project_id}",
    )


def reject_priority_zero_conflicts(
    firewalls: Iterable[Any],
    *,
    network_url: str,
    operator_prefixes: Sequence[str],
    target_tag: str | None,
) -> None:
    """Reject applicable priority-zero allows and operator-route denies."""
    allow_conflicts: list[str] = []
    deny_conflicts: list[str] = []
    for firewall in firewalls:
        if not _is_relevant_priority_zero(firewall, network_url=network_url, target_tag=target_tag):
            continue
        if firewall.allowed:
            allow_conflicts.append(str(firewall.name))
        if firewall.denied and _deny_overlaps_operator_ssh(firewall, operator_prefixes):
            deny_conflicts.append(str(firewall.name))

    if allow_conflicts or deny_conflicts:
        parts = []
        if allow_conflicts:
            parts.append(f"priority-zero ingress allow: {', '.join(sorted(allow_conflicts))}")
        if deny_conflicts:
            parts.append(f"priority-zero operator-SSH deny: {', '.join(sorted(deny_conflicts))}")
        raise ConfigError(
            f"GCE classic firewall conflict ({'; '.join(parts)})",
            hint="remove or retarget the conflicting priority-zero rules before using this gcp-gce site",
        )


def require_firewall_name_available(client: Any, *, project_id: str, rule_name: str) -> None:
    """Fail before mutation when an exact stable firewall name exists."""
    existing = call_google_optional(
        lambda: client.get(project=project_id, firewall=rule_name),
        operation="checking the firewall rule name",
        resource=f"firewall rule {project_id}/{rule_name}",
    )
    if existing is not None:
        raise AlreadyExistsError(
            f"GCE firewall rule '{rule_name}' already exists in project '{project_id}'",
            entity_kind="gcp-firewall-rule",
            entity_name=rule_name,
            hint="choose another VM name or inspect and remove known Agentworks residue",
        )


def reconcile_firewall(
    client: Any,
    *,
    project_id: str,
    expected: Any,
    ownership: FirewallOwnership | None,
) -> FirewallState:
    """Resolve an exact-name rule only when its provider incarnation is owned."""
    actual = call_google_optional(
        lambda: client.get(project=project_id, firewall=expected.name),
        operation="reconciling a possible firewall rule",
        resource=f"firewall rule {project_id}/{expected.name}",
    )
    if actual is None:
        return FirewallState.ABSENT
    if ownership is None:
        return FirewallState.INDETERMINATE
    if (
        str(actual.name) == ownership.rule_name
        and _provider_resource_id(actual.id) == ownership.resource_id
        and FirewallShape.from_resource(actual) == FirewallShape.from_resource(expected)
    ):
        return FirewallState.REALIZED
    return FirewallState.MISMATCHED


def insert_firewall_reconciled(
    client: Any,
    *,
    project_id: str,
    firewall: Any,
    attempt: FirewallInsertAttempt,
    timeout: float,
) -> FirewallOwnership:
    """Insert once and prove ownership through request, operation, and resource IDs."""
    from google.cloud import compute_v1

    if attempt.rule_name != str(firewall.name) or attempt.submitted:
        raise ValueError("firewall insert attempt does not match this fresh rule mutation")
    request = compute_v1.InsertFirewallRequest(
        project=project_id,
        firewall_resource=firewall,
        request_id=attempt.request_id,
    )
    attempt.submitted = True

    operation: Any | None = None
    initial_failure: AgentworksError | None = None
    try:
        operation = call_google(
            lambda: client.insert(request=request, retry=None),
            operation="inserting an owned firewall rule",
            resource=f"firewall rule {project_id}/{firewall.name}",
        )
    except (AlreadyExistsError, AuthorizationError, NotFoundError, TokenRejectedError, GCEQuotaError):
        raise
    except (ConnectivityError, GCEError) as exc:
        initial_failure = exc

    if operation is None:
        retry_failed = False
        try:
            # GCE documents same-requestId retry as the supported way to recover
            # a response lost before the operation identity reached the client.
            operation = call_google(
                lambda: client.insert(request=request, retry=None),
                operation="reconciling an indeterminate firewall insert",
                resource=f"firewall rule {project_id}/{firewall.name}",
            )
        except (AlreadyExistsError, AuthorizationError, NotFoundError, TokenRejectedError, GCEQuotaError):
            raise
        except (ConnectivityError, GCEError):
            retry_failed = True
        if retry_failed:
            if initial_failure is None:  # pragma: no cover - guarded by operation being absent
                raise AssertionError("missing initial firewall insert failure")
            raise initial_failure

    # Capture the provider incarnation before the first interruptible wait.
    # A KeyboardInterrupt from operation.result must leave rollback enough
    # identity to delete only this attempt's realized resource.
    attempt.ownership = _ownership_from_operation(
        operation,
        request_id=attempt.request_id,
        project_id=project_id,
        rule_name=attempt.rule_name,
    )

    wait_failure: AgentworksError | None = None
    try:
        wait_for_extended_operation(operation, label=f"firewall rule {firewall.name}", timeout=timeout)
    except (AlreadyExistsError, AuthorizationError, NotFoundError, TokenRejectedError, GCEQuotaError):
        raise
    except GCEOperationError as exc:
        wait_failure = exc

    state = reconcile_firewall(
        client,
        project_id=project_id,
        expected=firewall,
        ownership=attempt.ownership,
    )
    if state is FirewallState.REALIZED:
        return attempt.ownership
    if state is FirewallState.MISMATCHED:
        raise AlreadyExistsError(
            f"GCE firewall rule '{firewall.name}' has a different provider identity or shape after insert",
            entity_kind="gcp-firewall-rule",
            entity_name=str(firewall.name),
            hint="retain the colliding rule because this insert attempt does not own its provider identity",
        )
    if wait_failure is not None:
        raise wait_failure
    raise GCEOperationError(
        f"GCE firewall rule '{firewall.name}' was absent after its insert operation completed",
        entity_kind="gcp-firewall-rule",
        entity_name=str(firewall.name),
    )


def delete_matching_firewall(
    client: Any,
    *,
    project_id: str,
    expected: Any,
    ownership: FirewallOwnership | None,
    timeout: float,
) -> FirewallState:
    """Delete only a verified provider incarnation, then prove the result.

    GCE delete accepts only a rule name, not a provider-ID precondition. The
    ownership read therefore closes concurrent identical inserts but is not an
    atomic defense against a hostile delete/recreate between this read and the
    name-based delete.
    """
    try:
        state = reconcile_firewall(
            client,
            project_id=project_id,
            expected=expected,
            ownership=ownership,
        )
    except AgentworksError:
        return FirewallState.INDETERMINATE
    if state is not FirewallState.REALIZED:
        return state

    try:
        operation = call_google(
            lambda: client.delete(project=project_id, firewall=expected.name),
            operation="deleting an owned firewall rule",
            resource=f"firewall rule {project_id}/{expected.name}",
        )
        wait_for_extended_operation(operation, label=f"firewall rule {expected.name}", timeout=timeout)
    except AgentworksError:
        pass

    try:
        return reconcile_firewall(
            client,
            project_id=project_id,
            expected=expected,
            ownership=ownership,
        )
    except AgentworksError:
        return FirewallState.INDETERMINATE


def _ownership_from_operation(
    operation: Any,
    *,
    request_id: str,
    project_id: str,
    rule_name: str,
) -> FirewallOwnership:
    """Verify the GCE operation belongs to this request and capture its target."""
    expected_link = f"projects/{project_id}/global/firewalls/{rule_name}"
    resource_id = _provider_resource_id(getattr(operation, "target_id", None))
    if (
        str(getattr(operation, "client_operation_id", "")) != request_id
        or str(getattr(operation, "operation_type", "")).lower() != "insert"
        or _canonical_resource_url(str(getattr(operation, "target_link", ""))) != expected_link
        or resource_id is None
    ):
        raise GCEOperationError(
            f"Google Cloud returned incomplete ownership identity for firewall rule {project_id}/{rule_name}",
            entity_kind="gcp-firewall-rule",
            entity_name=rule_name,
            hint="retain the named rule until its provider identity can be established",
        )
    return FirewallOwnership(rule_name=rule_name, resource_id=resource_id)


def _provider_resource_id(value: object) -> str | None:
    """Normalize one positive uint64 provider ID without accepting blank/zero."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    try:
        normalized = int(value)
    except ValueError:
        return None
    return str(normalized) if normalized > 0 else None


def _canonical_resource_url(value: str) -> str:
    marker = "/projects/"
    if marker in value:
        return value[value.index(marker) + 1 :].rstrip("/")
    return value.lstrip("/").rstrip("/")


def _protocols(entries: Iterable[Any]) -> tuple[ProtocolPorts, ...]:
    return tuple(
        sorted(
            (
                ProtocolPorts(str(entry.I_p_protocol).lower(), tuple(sorted(str(port) for port in entry.ports)))
                for entry in entries
            ),
            key=lambda entry: (entry.protocol, entry.ports),
        )
    )


def _is_relevant_priority_zero(firewall: Any, *, network_url: str, target_tag: str | None) -> bool:
    if bool(firewall.disabled) or str(firewall.direction or "INGRESS").upper() != "INGRESS":
        return False
    if int(firewall.priority) != 0 or _canonical_resource_url(str(firewall.network)) != _canonical_resource_url(
        network_url
    ):
        return False
    target_tags = {str(value) for value in firewall.target_tags}
    target_accounts = {str(value) for value in firewall.target_service_accounts}
    if not target_tags and not target_accounts:
        return True
    return target_tag is not None and target_tag in target_tags


def _deny_overlaps_operator_ssh(firewall: Any, operator_prefixes: Sequence[str]) -> bool:
    if not any(_protocol_blocks_ssh(entry) for entry in firewall.denied):
        return False
    source_ranges = [str(value) for value in firewall.source_ranges]
    if not source_ranges:
        if firewall.source_tags or firewall.source_service_accounts:
            return False
        source_ranges = ["0.0.0.0/0"]
    try:
        sources = [ipaddress.ip_network(value, strict=False) for value in source_ranges]
        operators = [ipaddress.ip_network(value, strict=False) for value in operator_prefixes]
    except ValueError:
        return True
    return any(
        source.version == operator.version and source.overlaps(operator) for source in sources for operator in operators
    )


def _protocol_blocks_ssh(entry: Any) -> bool:
    protocol = str(entry.I_p_protocol).lower()
    if protocol == "all":
        return True
    if protocol not in {"tcp", "6"}:
        return False
    ports = [str(value) for value in entry.ports]
    if not ports:
        return True
    for port in ports:
        start, separator, end = port.partition("-")
        try:
            lower = int(start)
            upper = int(end) if separator else lower
            if lower <= 22 <= upper:
                return True
        except ValueError:
            return True
    return False
