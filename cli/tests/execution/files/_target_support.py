"""Test-only managed target identities bound to operation scopes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind

if TYPE_CHECKING:
    from agentworks.operations import OperationOwner


_INCARNATION = f"v1:{'0' * 64}"
_BOOT_ID = "00000000-0000-4000-8000-000000000001"


def target_for_owner(owner: OperationOwner) -> ManagedTargetIdentity:
    """Build the target identity matching a test operation's exact scope."""
    scope = owner.ownership.scope
    return ManagedTargetIdentity(
        ManagedTargetKind(scope.resource_kind.value),
        scope.resource_name,
        _INCARNATION,
        _BOOT_ID,
    )
