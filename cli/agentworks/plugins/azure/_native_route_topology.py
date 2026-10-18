"""Observed core-created Azure NIC/NSG topology and exact owned rule content."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from agentworks.errors import StateError, ValidationError
from agentworks.plugins.azure._identity import (
    ObservedResourceKind,
    canonical_vm_id,
    observed_resource_id_equal,
    parse_observed_resource_id,
    parse_resource_id,
    resource_name_equal,
)


def same_vm_id(observed: object, expected: str) -> bool:
    try:
        return observed_resource_id_equal(observed, expected, "virtualMachines")
    except ValidationError:
        return False


def network_id(value: object, resource_id: str, kind: ObservedResourceKind) -> str:
    """Validate a literal same-subscription provider link, preserving its bytes."""
    if type(value) is not str:
        raise ValidationError("Azure route network identity is invalid")
    subscription, _, _ = parse_resource_id(resource_id)
    linked_subscription, _, _ = parse_observed_resource_id(value, kind)
    if not resource_name_equal(linked_subscription, subscription):
        raise ValidationError("Azure route network subscription changed")
    return value


def vm_nic(observed: Any, resource_id: str, vm_id: str) -> str:
    if not same_vm_id(getattr(observed, "id", None), resource_id):
        raise StateError("Azure route VM path changed")
    if canonical_vm_id(getattr(observed, "vm_id", None)) != vm_id:
        raise StateError("Azure route VM incarnation changed")
    interfaces = getattr(getattr(observed, "network_profile", None), "network_interfaces", None)
    if not isinstance(interfaces, list) or len(interfaces) != 1:
        raise StateError("Azure route requires exactly one core-created NIC")
    return network_id(getattr(interfaces[0], "id", None), resource_id, "networkInterfaces")


def properties(value: Any) -> dict[str, Any]:
    if type(value) is not dict or type(value.get("properties")) is not dict:
        raise ValidationError("Azure route resource properties are invalid")
    return cast("dict[str, Any]", value["properties"])


def _resource(value: dict[str, Any], expected_id: str, resource_id: str, kind: ObservedResourceKind) -> dict[str, Any]:
    observed_id = network_id(value.get("id"), resource_id, kind)
    props = properties(value)
    tags = value.get("tags")
    if (
        not observed_resource_id_equal(observed_id, expected_id, kind)
        or type(tags) is not dict
        or tags.get("owner") != "agentworks"
        or props.get("provisioningState") != "Succeeded"
    ):
        raise StateError("Azure route network ownership or provisioning changed")
    canonical_vm_id(props.get("resourceGuid"))
    return props


@dataclass(frozen=True, slots=True)
class RouteTopology:
    nic_id: str
    nic_guid: str
    nsg_id: str
    nsg_guid: str


def observe_nic(value: dict[str, Any], nic_id: str, resource_id: str) -> tuple[str, str]:
    props = _resource(value, nic_id, resource_id, "networkInterfaces")
    vm = props.get("virtualMachine")
    if type(vm) is not dict or not same_vm_id(vm.get("id"), resource_id):
        raise StateError("Azure route NIC no longer belongs to the selected VM")
    nsg = props.get("networkSecurityGroup")
    if type(nsg) is not dict:
        raise StateError("Azure route NIC has no security group")
    return (
        network_id(nsg.get("id"), resource_id, "networkSecurityGroups"),
        canonical_vm_id(props["resourceGuid"]),
    )


def observe_nsg(value: dict[str, Any], nic_id: str, nsg_id: str, resource_id: str) -> str:
    props = _resource(value, nsg_id, resource_id, "networkSecurityGroups")
    refs = props.get("networkInterfaces")
    if (
        type(refs) is not list
        or len(refs) != 1
        or type(refs[0]) is not dict
        or not observed_resource_id_equal(refs[0].get("id"), nic_id, "networkInterfaces")
        or props.get("subnets", []) != []
    ):
        raise StateError("Azure route refuses a shared security group")
    return canonical_vm_id(props["resourceGuid"])


def rule_properties(priority: int, nonce: str, prefixes: tuple[str, ...]) -> dict[str, Any]:
    return {
        "priority": priority,
        "description": f"agentworks-native:{nonce}",
        "protocol": "Tcp",
        "sourcePortRange": "*",
        "destinationPortRange": "22",
        "sourceAddressPrefixes": list(prefixes),
        "destinationAddressPrefix": "*",
        "access": "Allow",
        "direction": "Inbound",
    }


def _matches(props: dict[str, Any], expected: dict[str, Any]) -> bool:
    # Alternate selectors must not broaden a rule whose primary fields match.
    for key in (
        "sourcePortRanges",
        "destinationPortRanges",
        "sourceAddressPrefixes",
        "destinationAddressPrefixes",
        "sourceApplicationSecurityGroups",
        "destinationApplicationSecurityGroups",
        "sourceAddressPrefix",
    ):
        if key not in expected and props.get(key) not in (None, [], ""):
            return False
    return all(type(props.get(key)) is type(item) and props[key] == item for key, item in expected.items())


def rule_inventory(nsg: dict[str, Any]) -> tuple[set[int], set[str]]:
    rules = properties(nsg).get("securityRules")
    if type(rules) is not list:
        raise ValidationError("Azure route rule inventory is incomplete")
    baseline = {
        "protocol": "*",
        "sourcePortRange": "*",
        "destinationPortRange": "*",
        "sourceAddressPrefix": "*",
        "destinationAddressPrefix": "*",
        "access": "Deny",
        "priority": 200,
        "direction": "Inbound",
    }
    baseline_count = 0
    used: set[int] = set()
    names: set[str] = set()
    for rule in rules:
        props = properties(rule)
        observed_name = rule.get("name")
        if type(observed_name) is not str or any(resource_name_equal(observed_name, prior) for prior in names):
            raise ValidationError("Azure route rule inventory identities conflict")
        names.add(observed_name)
        priority = props.get("priority")
        if (
            type(priority) is not int
            or not 100 <= priority <= 4096
            or props.get("direction") not in {"Inbound", "Outbound"}
        ):
            raise ValidationError("Azure route rule inventory is invalid")
        if props["direction"] == "Inbound":
            if priority in used:
                raise ValidationError("Azure route inbound priorities conflict")
            used.add(priority)
        if resource_name_equal(observed_name, "deny-all-inbound"):
            if not _matches(props, baseline):
                raise StateError("Azure route deny baseline changed")
            baseline_count += 1
    if baseline_count != 1:
        raise StateError("Azure route requires its deny baseline")
    return used, names


def select_priority(nsg: dict[str, Any], name: str) -> int:
    used, names = rule_inventory(nsg)
    if any(resource_name_equal(name, prior) for prior in names):
        raise StateError("Azure route requires an unused nonce")
    for priority in range(100, 200):
        if priority not in used:
            return priority
    raise StateError("Azure route has no free inbound priority")


def validate_rule(
    value: dict[str, Any], rule_id: str, name: str, expected: dict[str, Any], *, completed_failure: bool = False
) -> None:
    identity = value.get("id")
    if (
        type(identity) is not str
        or not resource_name_equal(identity, rule_id)
        or value.get("name") != name
        or not _matches(properties(value), expected)
        or properties(value).get("provisioningState")
        not in ({"Succeeded", "Failed", "Canceled"} if completed_failure else {"Succeeded"})
    ):
        raise StateError("Azure route rule identity or content changed")
