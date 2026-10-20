"""Bounded classic-firewall observations for GCE creation's existing network."""

from __future__ import annotations

from ipaddress import IPv4Network
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from agentworks.errors import StateError, ValidationError
from agentworks.plugins.gcp._creation_shapes import component, exact_resource
from agentworks.plugins.gcp._native_identity import canonical_path
from agentworks.plugins.gcp._native_route_evidence import numeric_id

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.gcp._creation_api import CreationAPI

FIELDS = (
    "kind,id,name,selfLink,network,direction,disabled,priority,targetTags,targetServiceAccounts,"
    "sourceRanges,sourceTags,sourceServiceAccounts,destinationRanges,allowed,denied"
)


def _strings(value: object) -> tuple[str, ...]:
    if (
        type(value) is not list or len(value) > 256
        or any(type(item) is not str or not item or len(item) > 1024 for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValidationError("GCE creation firewall string set is invalid")
    return tuple(sorted(value))


def _protocols(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if type(value) is not list or len(value) > 256:
        raise ValidationError("GCE creation firewall protocols are invalid")
    result: list[tuple[str, tuple[str, ...]]] = []
    for entry in value:
        if type(entry) is not dict or set(entry) - {"IPProtocol", "ports"}:
            raise ValidationError("GCE creation firewall protocol is invalid")
        protocol = entry.get("IPProtocol")
        if type(protocol) is not str or not (
            protocol in {"all", "tcp", "udp", "icmp", "esp", "ah", "ipip", "sctp"}
            or (protocol.isascii() and protocol.isdecimal() and str(int(protocol)) == protocol and int(protocol) <= 255)
        ):
            raise ValidationError("GCE creation firewall protocol is invalid")
        ports = _strings(entry.get("ports", []))
        for port in ports:
            parts = port.split("-")
            if (
                len(parts) not in (1, 2)
                or any(not p.isascii() or not p.isdecimal() or str(int(p)) != p for p in parts)
                or not 0 <= int(parts[0]) <= int(parts[-1]) <= 65535
            ):
                raise ValidationError("GCE creation firewall port is invalid")
        result.append((protocol, ports))
    if len(set(result)) != len(result):
        raise ValidationError("GCE creation firewall protocols are duplicated")
    return tuple(sorted(result))


def firewall_shape(value: dict[str, Any], project: str) -> tuple[Any, ...]:
    """Validate provider firewall behavior and compare defaults semantically."""
    direction = value.get("direction", "INGRESS")
    disabled, priority = value.get("disabled", False), value.get("priority", 1000)
    if (
        direction not in ("INGRESS", "EGRESS") or type(disabled) is not bool
        or type(priority) is not int or not 0 <= priority <= 65535
    ):
        raise ValidationError("GCE creation firewall behavior is invalid")
    result = (
        canonical_path(value.get("network"), project_id=project, kind="network"), direction, disabled, priority,
        *(_strings(value.get(key, [])) for key in (
            "targetTags", "targetServiceAccounts", "sourceRanges", "sourceTags",
            "sourceServiceAccounts", "destinationRanges"
        )),
        _protocols(value.get("allowed", [])), _protocols(value.get("denied", [])),
    )
    if bool(result[10]) == bool(result[11]) or (result[4] and result[5]):
        raise ValidationError("GCE creation firewall behavior is contradictory")
    return result


def validate_firewall(value: dict[str, Any], body: dict[str, Any], project: str, resource_id: str) -> None:
    exact_resource(value, f"projects/{project}/global/firewalls/{body['name']}", "firewall", resource_id)
    if firewall_shape(value, project) != firewall_shape(body, project):
        raise StateError("GCE created firewall contents changed")


def check_policy(
    api: CreationAPI, project: str, network: str, tag: str, prefixes: tuple[str, ...], deadline: Deadline,
    *, owned: dict[str, tuple[str, dict[str, Any]]],
) -> None:
    """Prove complete bounded classic inventory and refuse applicable conflicts."""
    value = api.get(network, deadline)
    assert value is not None
    if (
        value.get("kind") != "compute#network"
        or canonical_path(value.get("selfLink"), project_id=project, kind="network") != network
        or value.get("networkFirewallPolicyEnforcementOrder", "AFTER_CLASSIC_FIREWALL") != "AFTER_CLASSIC_FIREWALL"
    ):
        raise StateError("GCE creation requires the selected classic-first network")
    tokens: set[str] = set()
    names: set[str] = set()
    ids: set[str] = set()
    token: str | None = None
    for _page in range(64):
        query = {"maxResults": "100", "fields": f"items({FIELDS}),nextPageToken,warning(code)"}
        if token is not None:
            query["pageToken"] = token
        page = api.get(f"projects/{project}/global/firewalls?{urlencode(query)}", deadline)
        assert page is not None
        items = page.get("items", [])
        if "warning" in page or type(items) is not list or len(items) > 100 or len(names) + len(items) > 4096:
            raise ValidationError("GCE creation firewall inventory is incomplete or oversized")
        for item in items:
            if type(item) is not dict:
                raise ValidationError("GCE creation firewall inventory item is invalid")
            name, resource_id = component(item.get("name")), numeric_id(item.get("id"))
            exact_resource(item, f"projects/{project}/global/firewalls/{name}", "firewall", resource_id)
            if name in names or resource_id in ids:
                raise ValidationError("GCE creation firewall inventory has duplicate identities")
            names.add(name)
            ids.add(resource_id)
            if name in owned:
                expected_id, body = owned[name]
                validate_firewall(item, body, project, expected_id)
                continue
            behavior = firewall_shape(item, project)
            if behavior[0] != network or behavior[1] != "INGRESS" or behavior[2]:
                continue
            tags, accounts = behavior[4], behavior[5]
            if (tags and tag not in tags) or accounts:
                continue
            allowed, denied = behavior[10], behavior[11]
            if behavior[3] == 0 and allowed:
                raise StateError("GCE creation conflicts with an applicable priority-zero allow")
            denies_ssh = any(
                protocol == "all" or (protocol in ("tcp", "6") and (
                    not ports or any(int(port.split("-")[0]) <= 22 <= int(port.split("-")[-1]) for port in ports)
                )) for protocol, ports in denied
            )
            try:
                overlaps = any(
                    IPv4Network(source).overlaps(IPv4Network(prefix))
                    for source in (behavior[6] or ("0.0.0.0/0",)) for prefix in prefixes
                )
            except ValueError:
                raise ValidationError("GCE creation firewall source range is invalid") from None
            if denies_ssh and (behavior[7] or behavior[8] or overlaps):
                raise StateError("GCE creation conflicts with an applicable SSH deny")
        token = page.get("nextPageToken")
        if token is None or token == "":
            if not set(owned).issubset(names):
                raise StateError("GCE created firewall is missing from inventory")
            return
        if type(token) is not str or len(token) > 4096 or token in tokens:
            raise ValidationError("GCE creation firewall inventory token is invalid")
        tokens.add(token)
    raise ValidationError("GCE creation firewall inventory exceeds its page bound")
