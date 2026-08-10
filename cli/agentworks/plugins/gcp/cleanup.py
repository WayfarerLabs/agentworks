"""Bounded GCE create rollback and first/second interrupt handling."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, NoReturn

from agentworks import output
from agentworks.plugins.gcp.errors import GCEError, call_google, call_google_optional, wait_for_extended_operation
from agentworks.plugins.gcp.network import (
    FirewallOwnership,
    FirewallState,
    delete_matching_firewall,
    reconcile_firewall,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


class InstanceState(Enum):
    """What cleanup can prove about the possibly inserted instance."""

    ABSENT = "absent"
    SURVIVING = "surviving"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class CleanupCoordinates:
    """Every exact provider identity needed for safe manual recovery."""

    project_id: str
    zone: str
    instance_name: str
    allow_rule: str
    deny_rule: str


@dataclass(frozen=True)
class RollbackReport:
    """The provider state proven after one bounded rollback attempt."""

    instance: InstanceState
    allow: FirewallState
    deny: FirewallState

    @property
    def clean(self) -> bool:
        return (
            self.instance is InstanceState.ABSENT
            and self.allow is FirewallState.ABSENT
            and self.deny is FirewallState.ABSENT
        )


def probe_instance_state(
    instances: Any,
    *,
    coordinates: CleanupCoordinates,
) -> InstanceState:
    """Prove absence, survival, or indeterminacy by exact-name get."""
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
    except GCEError:
        return InstanceState.INDETERMINATE
    return InstanceState.ABSENT if instance is None else InstanceState.SURVIVING


def delete_instance_and_verify(
    instances: Any,
    *,
    coordinates: CleanupCoordinates,
    timeout: float,
) -> InstanceState:
    """Request exact instance deletion, then independently prove absence."""
    if probe_instance_state(instances, coordinates=coordinates) is InstanceState.ABSENT:
        return InstanceState.ABSENT
    try:
        operation = call_google(
            lambda: instances.delete(
                project=coordinates.project_id,
                zone=coordinates.zone,
                instance=coordinates.instance_name,
            ),
            operation="deleting the partial instance",
            resource=f"instance {coordinates.project_id}/{coordinates.zone}/{coordinates.instance_name}",
        )
        wait_for_extended_operation(operation, label=f"instance {coordinates.instance_name}", timeout=timeout)
    except GCEError:
        pass
    return probe_instance_state(instances, coordinates=coordinates)


def rollback_partial_create(
    *,
    instances: Any,
    firewalls: Any,
    coordinates: CleanupCoordinates,
    expected_allow: Any,
    expected_deny: Any,
    allow_ownership: FirewallOwnership | None,
    deny_ownership: FirewallOwnership | None,
    instance_possible: bool,
    timeout: float,
) -> RollbackReport:
    """Close allow, remove possible instance, then conditionally remove deny."""
    allow_state = delete_matching_firewall(
        firewalls,
        project_id=coordinates.project_id,
        expected=expected_allow,
        ownership=allow_ownership,
        timeout=timeout,
    )
    instance_state = (
        delete_instance_and_verify(instances, coordinates=coordinates, timeout=timeout)
        if instance_possible
        else InstanceState.ABSENT
    )
    if instance_state is InstanceState.ABSENT:
        deny_state = delete_matching_firewall(
            firewalls,
            project_id=coordinates.project_id,
            expected=expected_deny,
            ownership=deny_ownership,
            timeout=timeout,
        )
    else:
        deny_state = _inspect_firewall(
            firewalls,
            coordinates.project_id,
            expected_deny,
            deny_ownership,
        )
    return RollbackReport(instance_state, allow_state, deny_state)


def rollback_then_raise(
    primary: Exception,
    rollback: Callable[[], RollbackReport],
    coordinates: CleanupCoordinates,
) -> NoReturn:
    """Run bounded cleanup without replacing an ordinary primary failure."""
    report: RollbackReport | None = None
    cleanup_failed = False
    try:
        report = rollback()
    except Exception:
        cleanup_failed = True
    if cleanup_failed:
        output.warn("GCE rollback failed unexpectedly; provider resources may remain.")
    if report is not None and not report.clean:
        output.warn(_manual_guidance(coordinates, report))
    raise primary


def rollback_after_interrupt(
    primary: KeyboardInterrupt,
    rollback: Callable[[], RollbackReport],
    coordinates: CleanupCoordinates,
) -> NoReturn:
    """Clean after the first interrupt; abandon promptly after the second."""
    output.warn(
        f"Interrupted: cleaning up partial GCE resources for '{coordinates.instance_name}', "
        "please wait (Ctrl-C again to abandon cleanup)..."
    )
    report: RollbackReport | None = None
    abandoned = False
    try:
        report = rollback()
    except KeyboardInterrupt:
        abandoned = True
    except Exception:
        report = None
    if abandoned:
        output.warn("Cleanup abandoned. " + _manual_guidance(coordinates, None))
    elif report is None:
        output.warn("GCE cleanup failed unexpectedly. " + _manual_guidance(coordinates, None))
    elif not report.clean:
        output.warn(_manual_guidance(coordinates, report))
    raise primary


def _inspect_firewall(
    firewalls: Any,
    project_id: str,
    expected: Any,
    ownership: FirewallOwnership | None,
) -> FirewallState:
    try:
        return reconcile_firewall(
            firewalls,
            project_id=project_id,
            expected=expected,
            ownership=ownership,
        )
    except GCEError:
        return FirewallState.INDETERMINATE


def _manual_guidance(coordinates: CleanupCoordinates, report: RollbackReport | None) -> str:
    instance_needs_action = report is None or report.instance is not InstanceState.ABSENT
    allow_needs_action = report is None or report.allow is not FirewallState.ABSENT
    deny_needs_action = report is None or report.deny is not FirewallState.ABSENT
    actions = [
        f"project '{coordinates.project_id}', zone '{coordinates.zone}', instance '{coordinates.instance_name}', "
        f"allow '{coordinates.allow_rule}', deny '{coordinates.deny_rule}'."
    ]
    if instance_needs_action:
        actions.append(
            f"Run `gcloud compute instances delete {coordinates.instance_name} "
            f"--project {coordinates.project_id} --zone {coordinates.zone}`."
        )
    for name, needed, state in (
        (coordinates.allow_rule, allow_needs_action, None if report is None else report.allow),
        (coordinates.deny_rule, deny_needs_action, None if report is None else report.deny),
    ):
        if not needed:
            continue
        actions.append(
            f"Inspect `gcloud compute firewall-rules describe {name} --project {coordinates.project_id}`; "
            f"delete it only if its complete shape is Agentworks-owned"
            + ("." if state is not FirewallState.MISMATCHED else " (the observed shape mismatched).")
        )
    return "Manual cleanup: " + " ".join(actions)
