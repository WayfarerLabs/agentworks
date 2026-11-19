"""Bounded GCE create rollback: what it removes, what it keeps, and what it proves.

The operator's side, the interrupt protocol and the recovery guidance, is
:mod:`agentworks.plugins.gcp.recovery`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from agentworks.errors import AgentworksError
from agentworks.plugins.gcp.compute import provider_resource_id, verify_zonal_operation
from agentworks.plugins.gcp.errors import call_google, call_google_optional, wait_for_extended_operation
from agentworks.plugins.gcp.network import (
    FirewallInsertAttempt,
    FirewallOwnership,
    FirewallReconciliation,
    FirewallState,
    delete_matching_firewall,
    inspect_firewall,
)

if TYPE_CHECKING:
    from typing import Any

    from agentworks.plugins.gcp.instance import InstanceInsertAttempt


class InstanceState(Enum):
    """What cleanup can prove about the possibly inserted instance."""

    ABSENT = "absent"
    COLLISION = "collision"
    SURVIVING = "surviving"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class InstanceOwnership:
    """One server-assigned instance incarnation safe to delete by name.

    The Phase 2 insert seam must derive this from the operation identity before
    its first interruptible wait, exactly as ``FirewallInsertAttempt`` does.
    """

    resource_id: str


@dataclass(frozen=True)
class CleanupCoordinates:
    """Every exact provider identity needed for safe manual recovery."""

    project_id: str
    zone: str
    instance_name: str
    allow_rule: str
    deny_rule: str


@dataclass(frozen=True)
class UnresolvedInsert:
    """An insert sent whose outcome is unknown, so its resource may still appear.

    ``operation_name`` is the insert's verified operation, or ``None`` when no
    answer to the insert arrived.
    """

    operation_name: str | None


@dataclass(frozen=True)
class UnresolvedInserts:
    """Which of a create's inserts may still create their resource."""

    instance: UnresolvedInsert | None = None
    allow: UnresolvedInsert | None = None
    deny: UnresolvedInsert | None = None

    @classmethod
    def from_attempts(
        cls,
        *,
        instance: InstanceInsertAttempt,
        allow: FirewallInsertAttempt,
        deny: FirewallInsertAttempt,
    ) -> UnresolvedInserts:
        return cls(_unresolved(instance), _unresolved(allow), _unresolved(deny))

    @property
    def any(self) -> bool:
        return self != UnresolvedInserts()


def _unresolved(attempt: InstanceInsertAttempt | FirewallInsertAttempt) -> UnresolvedInsert | None:
    return UnresolvedInsert(attempt.operation_name) if attempt.outcome_unknown else None


@dataclass(frozen=True)
class CleanupTarget:
    """Everything cleanup and its guidance know about one VM's resources.

    The exact names, each resource's owned provider incarnation where it is
    known, and which inserts may still create their resource.
    """

    coordinates: CleanupCoordinates
    instance_ownership: InstanceOwnership | None
    allow_ownership: FirewallOwnership | None
    deny_ownership: FirewallOwnership | None
    unresolved: UnresolvedInserts = UnresolvedInserts()


@dataclass(frozen=True)
class RollbackReport:
    """The provider state proven after one bounded rollback attempt.

    ``may_appear`` names each resource an unresolved insert may still create:
    one rollback neither deleted nor saw as its owned incarnation, so its
    absence, or a failed read of it, is not final.
    """

    instance: InstanceState
    allow: FirewallState
    deny: FirewallState
    instance_resource_id: str | None = None
    allow_resource_id: str | None = None
    deny_resource_id: str | None = None
    may_appear: UnresolvedInserts = UnresolvedInserts()

    @property
    def clean(self) -> bool:
        return (
            self.instance is InstanceState.ABSENT
            and self.allow is FirewallState.ABSENT
            and self.deny is FirewallState.ABSENT
            and not self.may_appear.any
        )


@dataclass(frozen=True)
class InstanceReconciliation:
    """An instance's state after cleanup and the provider ID observed for its name.

    ``seen_owned`` says cleanup saw the owned incarnation, before or after
    deleting it, so an insert that created it has landed.
    """

    state: InstanceState
    observed_resource_id: str | None
    seen_owned: bool = False


def _read_instance(
    instances: Any,
    *,
    coordinates: CleanupCoordinates,
) -> tuple[InstanceState, Any | None]:
    """Read one exact-name instance without turning uncertainty into absence."""
    try:
        instance = call_google_optional(
            lambda: instances.get(
                project=coordinates.project_id,
                zone=coordinates.zone,
                instance=coordinates.instance_name,
            ),
            operation="checking instance cleanup",
            resource=f"instance {coordinates.project_id}/{coordinates.zone}/{coordinates.instance_name}",
        )
    except AgentworksError:
        return InstanceState.INDETERMINATE, None
    if instance is None:
        return InstanceState.ABSENT, None
    return InstanceState.SURVIVING, instance


def _reconcile_instance(
    instances: Any,
    *,
    coordinates: CleanupCoordinates,
    ownership: InstanceOwnership | None,
) -> tuple[InstanceState, str | None]:
    """Classify an exact-name instance and retain its observed provider ID."""
    state, instance = _read_instance(instances, coordinates=coordinates)
    if state is not InstanceState.SURVIVING:
        return state, None
    if instance is None:  # pragma: no cover - paired by _read_instance
        return InstanceState.INDETERMINATE, None
    observed_id = provider_resource_id(instance.id)
    if ownership is None or observed_id is None:
        return InstanceState.INDETERMINATE, observed_id
    if observed_id != ownership.resource_id:
        return InstanceState.COLLISION, observed_id
    return InstanceState.SURVIVING, observed_id


def delete_instance_and_verify(
    instances: Any,
    *,
    coordinates: CleanupCoordinates,
    ownership: InstanceOwnership | None,
    timeout: float,
) -> InstanceReconciliation:
    """Delete only the expected provider incarnation, then prove absence."""
    state, observed_id = _reconcile_instance(
        instances,
        coordinates=coordinates,
        ownership=ownership,
    )
    if state is not InstanceState.SURVIVING:
        return InstanceReconciliation(state, observed_id)
    try:
        from google.cloud import compute_v1

        request_id = str(uuid.uuid4())
        operation = call_google(
            lambda: instances.delete(
                request=compute_v1.DeleteInstanceRequest(
                    project=coordinates.project_id,
                    zone=coordinates.zone,
                    instance=coordinates.instance_name,
                    request_id=request_id,
                ),
                retry=None,
            ),
            operation="deleting the partial instance",
            resource=f"instance {coordinates.project_id}/{coordinates.zone}/{coordinates.instance_name}",
        )
        verify_zonal_operation(
            operation,
            request_id=request_id,
            operation_type="delete",
            project_id=coordinates.project_id,
            zone=coordinates.zone,
            instance_name=coordinates.instance_name,
            expected_resource_id=None if ownership is None else ownership.resource_id,
        )
        wait_for_extended_operation(
            operation,
            label=f"instance {coordinates.instance_name}",
            zone=coordinates.zone,
            timeout=timeout,
        )
    except AgentworksError:
        pass
    state, observed_id = _reconcile_instance(
        instances,
        coordinates=coordinates,
        ownership=ownership,
    )
    return InstanceReconciliation(state, observed_id, seen_owned=True)


def rollback_partial_create(
    target: CleanupTarget,
    *,
    instances: Any,
    firewalls: Any,
    expected_allow: Any,
    expected_deny: Any,
    instance_possible: bool,
    timeout: float,
) -> RollbackReport:
    """Close allow, remove possible instance, then conditionally remove deny.

    The deny goes only once the instance is proven absent and no unresolved
    insert can still create it. Each resource an unresolved insert may
    still create is reported in ``may_appear``.
    """
    coordinates = target.coordinates
    allow_result = delete_matching_firewall(
        firewalls,
        project_id=coordinates.project_id,
        expected=expected_allow,
        ownership=target.allow_ownership,
        timeout=timeout,
    )
    instance_result = (
        delete_instance_and_verify(
            instances,
            coordinates=coordinates,
            ownership=target.instance_ownership,
            timeout=timeout,
        )
        if instance_possible
        else InstanceReconciliation(InstanceState.ABSENT, None)
    )
    instance_may_appear = _still_unresolved(target.unresolved.instance, settled=instance_result.seen_owned)
    if instance_result.state is InstanceState.ABSENT and instance_may_appear is None:
        deny_result = delete_matching_firewall(
            firewalls,
            project_id=coordinates.project_id,
            expected=expected_deny,
            ownership=target.deny_ownership,
            timeout=timeout,
        )
    else:
        deny_result = _inspect_firewall(
            firewalls,
            coordinates.project_id,
            expected_deny,
            target.deny_ownership,
        )
    return RollbackReport(
        instance_result.state,
        allow_result.state,
        deny_result.state,
        instance_result.observed_resource_id,
        allow_result.observed_resource_id,
        deny_result.observed_resource_id,
        may_appear=UnresolvedInserts(
            instance_may_appear,
            _still_unresolved(target.unresolved.allow, settled=allow_result.seen_owned),
            _still_unresolved(target.unresolved.deny, settled=deny_result.seen_owned),
        ),
    )


def _still_unresolved(insert: UnresolvedInsert | None, *, settled: bool) -> UnresolvedInsert | None:
    """``insert``, unless cleanup saw the owned incarnation it would create, so it has landed.

    A resource found absent, or one whose read failed, may still be created
    by the insert.
    """
    return None if settled else insert


def _inspect_firewall(
    firewalls: Any,
    project_id: str,
    expected: Any,
    ownership: FirewallOwnership | None,
) -> FirewallReconciliation:
    try:
        return inspect_firewall(
            firewalls,
            project_id=project_id,
            expected=expected,
            ownership=ownership,
        )
    except AgentworksError:
        return FirewallReconciliation(FirewallState.INDETERMINATE, None)
