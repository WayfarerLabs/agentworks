"""Strict selected classic firewall shapes and conservative conflict checks."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_network
from typing import Any

from agentworks.errors import StateError, ValidationError
from agentworks.plugins.gcp._native_identity import NativeIdentity, canonical_path
from agentworks.plugins.gcp._native_route_evidence import RoutePayload, numeric_id

RULE_FIELDS = (
    "kind,id,name,selfLink,description,network,direction,disabled,priority,"
    "targetTags,targetServiceAccounts,sourceRanges,sourceTags,sourceServiceAccounts,"
    "destinationRanges,allowed,denied"
)


def _strings(value: object, *, limit: int = 256) -> tuple[str, ...]:
    if (
        type(value) is not list
        or len(value) > limit
        or any(type(item) is not str or not item or len(item) > 1024 for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("invalid string set")
    return tuple(sorted(value))


def _ranges(value: object) -> tuple[str, ...]:
    ranges = _strings(value)
    if any(str(ip_network(item)) != item for item in ranges):
        raise ValueError("noncanonical address range")
    return ranges


def _protocols(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if type(value) is not list or len(value) > 256:
        raise ValueError("invalid protocols")
    result: list[tuple[str, tuple[str, ...]]] = []
    for item in value:
        if type(item) is not dict or set(item) - {"IPProtocol", "ports"}:
            raise ValueError("invalid protocol shape")
        protocol = item.get("IPProtocol")
        if type(protocol) is not str or not (
            protocol in {"all", "tcp", "udp", "icmp", "esp", "ah", "ipip", "sctp"}
            or (protocol.isascii() and protocol.isdecimal() and str(int(protocol)) == protocol and int(protocol) <= 255)
        ):
            raise ValueError("invalid protocol")
        ports = _strings(item.get("ports", []))
        for port in ports:
            endpoints = port.split("-")
            if (
                len(endpoints) not in (1, 2)
                or any(not p.isascii() or not p.isdecimal() or str(int(p)) != p for p in endpoints)
                or not 0 <= int(endpoints[0]) <= int(endpoints[-1]) <= 65535
                or protocol not in {"tcp", "udp", "sctp", "6", "17", "132"}
            ):
                raise ValueError("invalid ports")
        result.append((protocol, ports))
    if len(set(result)) != len(result):
        raise ValueError("duplicate protocol shape")
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True)
class FirewallShape:
    network: str
    direction: str
    disabled: bool
    priority: int
    target_tags: tuple[str, ...]
    target_accounts: tuple[str, ...]
    source_ranges: tuple[str, ...]
    source_tags: tuple[str, ...]
    source_accounts: tuple[str, ...]
    destination_ranges: tuple[str, ...]
    allowed: tuple[tuple[str, tuple[str, ...]], ...]
    denied: tuple[tuple[str, tuple[str, ...]], ...]


def shape(value: dict[str, Any], identity: NativeIdentity) -> FirewallShape:
    """Validate the provider's complete selected behavioral firewall projection."""
    try:
        result = FirewallShape(
            network=canonical_path(value.get("network"), project_id=identity.project_id, kind="network"),
            direction=value.get("direction", "INGRESS"),
            disabled=value.get("disabled", False),
            priority=value.get("priority", 1000),
            target_tags=_strings(value.get("targetTags", []), limit=64),
            target_accounts=_strings(value.get("targetServiceAccounts", [])),
            source_ranges=_ranges(value.get("sourceRanges", [])),
            source_tags=_strings(value.get("sourceTags", []), limit=64),
            source_accounts=_strings(value.get("sourceServiceAccounts", [])),
            destination_ranges=_ranges(value.get("destinationRanges", [])),
            allowed=_protocols(value.get("allowed", [])),
            denied=_protocols(value.get("denied", [])),
        )
        if (
            result.direction not in ("INGRESS", "EGRESS")
            or type(result.disabled) is not bool
            or type(result.priority) is not int
            or not 0 <= result.priority <= 65535
            or bool(result.allowed) == bool(result.denied)
            or (result.target_tags and result.target_accounts)
        ):
            raise ValueError("invalid firewall behavior")
        return result
    except (ValueError, TypeError, ValidationError):
        raise ValidationError("GCE firewall shape is invalid") from None


def rule_body(payload: RoutePayload) -> dict[str, Any]:
    return {
        "name": payload.name,
        "description": payload.description,
        "network": payload.identity.network_url,
        "direction": "INGRESS",
        "disabled": False,
        "priority": 0,
        "targetTags": [payload.identity.network_tag],
        "sourceRanges": list(payload.prefixes),
        "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
    }


def _identity(value: dict[str, Any], identity: NativeIdentity, name: str, expected_id: str) -> None:
    path = f"/compute/v1/projects/{identity.project_id}/global/firewalls/{name}"
    if (
        value.get("kind") != "compute#firewall"
        or value.get("name") != name
        or numeric_id(value.get("id")) != expected_id
        or (
            "selfLink" in value
            and value["selfLink"]
            not in tuple(f"https://{host}{path}" for host in ("compute.googleapis.com", "www.googleapis.com"))
        )
    ):
        raise StateError("GCE firewall incarnation changed")


def validate_owned(value: dict[str, Any], payload: RoutePayload) -> None:
    """Require receipt numeric ID plus the full effective shape and nonce."""
    if payload.rule_id is None:
        raise StateError("GCE route has no acknowledged firewall identity")
    _identity(value, payload.identity, payload.name, payload.rule_id)
    if value.get("description") != payload.description or shape(value, payload.identity) != shape(
        rule_body(payload), payload.identity
    ):
        raise StateError("GCE owned route contents changed")


def validate_baseline(value: dict[str, Any], identity: NativeIdentity) -> None:
    _identity(value, identity, identity.deny_rule, identity.deny_rule_id)
    expected = FirewallShape(
        identity.network_url,
        "INGRESS",
        False,
        1,
        (identity.network_tag,),
        (),
        ("0.0.0.0/0",),
        (),
        (),
        (),
        (),
        (("all", ()),),
    )
    if shape(value, identity) != expected:
        raise StateError("GCE baseline deny changed")


def validate_network(value: dict[str, Any], identity: NativeIdentity) -> None:
    """Omitted enforcement order uses the documented classic-first default."""
    if (
        value.get("kind") != "compute#network"
        or value.get("name") != identity.network_url.rsplit("/", 1)[1]
        or canonical_path(value.get("selfLink"), project_id=identity.project_id, kind="network") != identity.network_url
        or value.get("networkFirewallPolicyEnforcementOrder", "AFTER_CLASSIC_FIREWALL") != "AFTER_CLASSIC_FIREWALL"
    ):
        raise StateError("GCE route requires the selected classic-first network")


def conflict(value: dict[str, Any], payload: RoutePayload, tags: tuple[str, ...], accounts: tuple[str, ...]) -> None:
    """Refuse applicable priority-zero allows or denies that may block operator SSH."""
    rule = shape(value, payload.identity)
    if rule.network != payload.identity.network_url:
        return
    if rule.disabled or rule.direction != "INGRESS":
        return
    if rule.target_tags and not set(rule.target_tags).intersection(tags):
        return
    if rule.target_accounts and not set(rule.target_accounts).intersection(accounts):
        return
    if rule.priority == 0 and rule.allowed:
        raise StateError("GCE route conflicts with an applicable priority-zero allow")
    # Higher-priority allows cannot bypass an applicable priority-zero deny.
    # Other denies remain refused: the operator policy cannot be silently widened.
    ssh_denied = any(
        protocol == "all"
        or (
            protocol in ("tcp", "6")
            and (not ports or any(int(port.split("-")[0]) <= 22 <= int(port.split("-")[-1]) for port in ports))
        )
        for protocol, ports in rule.denied
    )
    if not ssh_denied or value.get("name") == payload.identity.deny_rule:
        return
    sources = rule.source_ranges or ("0.0.0.0/0",)
    if (
        rule.source_tags
        or rule.source_accounts
        or any(
            ip_network(source).version == 4 and ip_network(source).overlaps(IPv4)
            for source in sources
            for IPv4 in (ip_network(prefix) for prefix in payload.prefixes)
        )
    ):
        raise StateError("GCE route conflicts with an operator SSH deny")
