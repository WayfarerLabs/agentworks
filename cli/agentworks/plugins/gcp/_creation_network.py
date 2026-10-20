"""Bounded classic-firewall observations for GCE creation's existing network."""

from __future__ import annotations

from ipaddress import ip_network
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from agentworks.errors import StateError, ValidationError
from agentworks.plugins.gcp._creation_shapes import component, exact_resource
from agentworks.plugins.gcp._native_identity import canonical_path
from agentworks.plugins.gcp._native_route_evidence import numeric_id
from agentworks.plugins.gcp._native_route_topology import shape_for_project

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.gcp._creation_api import CreationAPI

FIELDS = (
    "kind,id,name,selfLink,network,direction,disabled,priority,targetTags,targetServiceAccounts,"
    "sourceRanges,sourceTags,sourceServiceAccounts,destinationRanges,allowed,denied"
)


def validate_firewall(value: dict[str, Any], body: dict[str, Any], project: str, resource_id: str) -> None:
    exact_resource(value, f"projects/{project}/global/firewalls/{body['name']}", "firewall", resource_id)
    if shape_for_project(value, project) != shape_for_project(body, project):
        raise StateError("GCE created firewall contents changed")


def check_policy(
    api: CreationAPI,
    project: str,
    network: str,
    tag: str,
    prefixes: tuple[str, ...],
    deadline: Deadline,
    *,
    owned: dict[str, tuple[str, dict[str, Any]]],
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
            behavior = shape_for_project(item, project)
            if behavior.network != network or behavior.direction != "INGRESS" or behavior.disabled:
                continue
            if (behavior.target_tags and tag not in behavior.target_tags) or behavior.target_accounts:
                continue
            if behavior.priority == 0 and behavior.allowed:
                raise StateError("GCE creation conflicts with an applicable priority-zero allow")
            denies_ssh = any(
                protocol == "all"
                or (
                    protocol in ("tcp", "6")
                    and (not ports or any(int(port.split("-")[0]) <= 22 <= int(port.split("-")[-1]) for port in ports))
                )
                for protocol, ports in behavior.denied
            )
            if not denies_ssh:
                continue
            overlaps = any(
                ip_network(source).version == 4 and ip_network(source).overlaps(ip_network(prefix))
                for source in (behavior.source_ranges or ("0.0.0.0/0",))
                for prefix in prefixes
            )
            if behavior.source_tags or behavior.source_accounts or overlaps:
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
