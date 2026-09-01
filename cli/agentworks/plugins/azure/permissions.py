"""Azure RBAC actions required to provision and roll back a VM."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

# Control-plane actions exercised by create and its fail-closed cleanup at the
# configured resource-group scope. Runtime power operations and transient-route
# security-rule reads/writes are intentionally outside this create-time check.
REQUIRED_RESOURCE_GROUP_ACTIONS = frozenset(
    {
        "Microsoft.Compute/disks/delete",
        "Microsoft.Compute/disks/read",
        "Microsoft.Compute/disks/write",
        "Microsoft.Compute/locations/operations/read",
        "Microsoft.Compute/virtualMachines/delete",
        "Microsoft.Compute/virtualMachines/read",
        "Microsoft.Compute/virtualMachines/write",
        "Microsoft.Network/locations/operationResults/read",
        "Microsoft.Network/locations/operations/read",
        "Microsoft.Network/networkInterfaces/delete",
        "Microsoft.Network/networkInterfaces/join/action",
        "Microsoft.Network/networkInterfaces/read",
        "Microsoft.Network/networkInterfaces/write",
        "Microsoft.Network/networkSecurityGroups/delete",
        "Microsoft.Network/networkSecurityGroups/join/action",
        "Microsoft.Network/networkSecurityGroups/read",
        "Microsoft.Network/networkSecurityGroups/securityRules/delete",
        "Microsoft.Network/networkSecurityGroups/write",
        "Microsoft.Network/publicIPAddresses/delete",
        "Microsoft.Network/publicIPAddresses/join/action",
        "Microsoft.Network/publicIPAddresses/read",
        "Microsoft.Network/publicIPAddresses/write",
        "Microsoft.Network/virtualNetworks/delete",
        "Microsoft.Network/virtualNetworks/read",
        "Microsoft.Network/virtualNetworks/subnets/join/action",
        "Microsoft.Network/virtualNetworks/subnets/write",
        "Microsoft.Network/virtualNetworks/write",
    }
)


def missing_resource_group_actions(permission_blocks: Iterable[object]) -> tuple[str, ...]:
    """Evaluate Azure ``Actions - NotActions`` blocks, then add their grants.

    The permission listing is provider input. Every block must carry well-formed
    action arrays before an omission can be treated as definitive. Iteration is
    never shortened when all grants have already appeared, so a later paging or
    response-shape failure remains indeterminate to the caller.
    """

    granted: set[str] = set()
    for block in permission_blocks:
        actions = _patterns(block, "actions")
        not_actions = _patterns(block, "not_actions")
        for required in REQUIRED_RESOURCE_GROUP_ACTIONS:
            if _matches_any(actions, required) and not _matches_any(not_actions, required):
                granted.add(required)

    return tuple(sorted(REQUIRED_RESOURCE_GROUP_ACTIONS - granted))


def _patterns(block: object, field: str) -> tuple[str, ...]:
    value = getattr(block, field, None)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"Azure permission block has no {field} array")

    patterns: list[str] = []
    for pattern in value:
        if not isinstance(pattern, str) or not pattern or pattern != pattern.strip():
            raise ValueError(f"Azure permission block has an invalid {field} entry")
        patterns.append(pattern.casefold())
    return tuple(patterns)


def _matches_any(patterns: tuple[str, ...], action: str) -> bool:
    normalized_action = action.casefold()
    return any(
        re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), normalized_action) is not None for pattern in patterns
    )
