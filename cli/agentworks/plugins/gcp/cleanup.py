"""Bounded GCE create rollback and first/second interrupt handling."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, NoReturn

from agentworks import output
from agentworks.errors import AgentworksError
from agentworks.plugins.gcp.compute import provider_resource_id, verify_zonal_operation
from agentworks.plugins.gcp.errors import call_google, call_google_optional, wait_for_extended_operation
from agentworks.plugins.gcp.network import (
    FirewallOwnership,
    FirewallReconciliation,
    FirewallState,
    delete_matching_firewall,
    inspect_firewall,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


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
class RollbackReport:
    """The provider state proven after one bounded rollback attempt."""

    instance: InstanceState
    allow: FirewallState
    deny: FirewallState
    instance_resource_id: str | None = None
    allow_resource_id: str | None = None
    deny_resource_id: str | None = None

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
    ownership: InstanceOwnership | None,
) -> InstanceState:
    """Prove absence, survival, or indeterminacy by exact-name get."""
    state, _observed_id = _reconcile_instance(
        instances,
        coordinates=coordinates,
        ownership=ownership,
    )
    return state


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
) -> tuple[InstanceState, str | None]:
    """Delete only the expected provider incarnation, then prove absence."""
    state, observed_id = _reconcile_instance(
        instances,
        coordinates=coordinates,
        ownership=ownership,
    )
    if state is not InstanceState.SURVIVING:
        return state, observed_id
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
        wait_for_extended_operation(operation, label=f"instance {coordinates.instance_name}", timeout=timeout)
    except AgentworksError:
        pass
    return _reconcile_instance(
        instances,
        coordinates=coordinates,
        ownership=ownership,
    )


def manual_cleanup_guidance(
    coordinates: CleanupCoordinates,
    report: RollbackReport | None,
    *,
    instance_ownership: InstanceOwnership | None,
    allow_ownership: FirewallOwnership | None,
    deny_ownership: FirewallOwnership | None,
) -> str:
    """Return provider-ID-gated recovery guidance for lifecycle callers."""
    return _manual_guidance(
        coordinates,
        report,
        instance_ownership=instance_ownership,
        allow_ownership=allow_ownership,
        deny_ownership=deny_ownership,
    )


def rollback_partial_create(
    *,
    instances: Any,
    firewalls: Any,
    coordinates: CleanupCoordinates,
    expected_allow: Any,
    expected_deny: Any,
    allow_ownership: FirewallOwnership | None,
    deny_ownership: FirewallOwnership | None,
    instance_ownership: InstanceOwnership | None,
    instance_possible: bool,
    timeout: float,
) -> RollbackReport:
    """Close allow, remove possible instance, then conditionally remove deny."""
    allow_result = delete_matching_firewall(
        firewalls,
        project_id=coordinates.project_id,
        expected=expected_allow,
        ownership=allow_ownership,
        timeout=timeout,
    )
    instance_state, instance_resource_id = (
        delete_instance_and_verify(
            instances,
            coordinates=coordinates,
            ownership=instance_ownership,
            timeout=timeout,
        )
        if instance_possible
        else (InstanceState.ABSENT, None)
    )
    if instance_state is InstanceState.ABSENT:
        deny_result = delete_matching_firewall(
            firewalls,
            project_id=coordinates.project_id,
            expected=expected_deny,
            ownership=deny_ownership,
            timeout=timeout,
        )
    else:
        deny_result = _inspect_firewall(
            firewalls,
            coordinates.project_id,
            expected_deny,
            deny_ownership,
        )
    return RollbackReport(
        instance_state,
        allow_result.state,
        deny_result.state,
        instance_resource_id,
        allow_result.observed_resource_id,
        deny_result.observed_resource_id,
    )


def rollback_then_raise(
    primary: Exception,
    rollback: Callable[[], RollbackReport],
    coordinates: CleanupCoordinates,
    *,
    instance_ownership: InstanceOwnership | None,
    allow_ownership: FirewallOwnership | None,
    deny_ownership: FirewallOwnership | None,
) -> NoReturn:
    """Run bounded cleanup without replacing an ordinary primary failure."""
    report: RollbackReport | None = None
    cleanup_failed = False
    try:
        report = rollback()
    except Exception:
        cleanup_failed = True
    if cleanup_failed:
        output.warn(
            "GCE rollback failed unexpectedly. "
            + _manual_guidance(
                coordinates,
                None,
                instance_ownership=instance_ownership,
                allow_ownership=allow_ownership,
                deny_ownership=deny_ownership,
            )
        )
    if report is not None and not report.clean:
        output.warn(
            _manual_guidance(
                coordinates,
                report,
                instance_ownership=instance_ownership,
                allow_ownership=allow_ownership,
                deny_ownership=deny_ownership,
            )
        )
    raise primary


def rollback_after_interrupt(
    primary: KeyboardInterrupt,
    rollback: Callable[[], RollbackReport],
    coordinates: CleanupCoordinates,
    *,
    instance_ownership: InstanceOwnership | None,
    allow_ownership: FirewallOwnership | None,
    deny_ownership: FirewallOwnership | None,
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
        output.warn(
            "Cleanup abandoned. "
            + _manual_guidance(
                coordinates,
                None,
                instance_ownership=instance_ownership,
                allow_ownership=allow_ownership,
                deny_ownership=deny_ownership,
            )
        )
    elif report is None:
        output.warn(
            "GCE cleanup failed unexpectedly. "
            + _manual_guidance(
                coordinates,
                None,
                instance_ownership=instance_ownership,
                allow_ownership=allow_ownership,
                deny_ownership=deny_ownership,
            )
        )
    elif not report.clean:
        output.warn(
            _manual_guidance(
                coordinates,
                report,
                instance_ownership=instance_ownership,
                allow_ownership=allow_ownership,
                deny_ownership=deny_ownership,
            )
        )
    raise primary


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


def _manual_guidance(
    coordinates: CleanupCoordinates,
    report: RollbackReport | None,
    *,
    instance_ownership: InstanceOwnership | None,
    allow_ownership: FirewallOwnership | None,
    deny_ownership: FirewallOwnership | None,
) -> str:
    instance_needs_action = report is None or report.instance is not InstanceState.ABSENT
    allow_needs_action = report is None or report.allow is not FirewallState.ABSENT
    deny_needs_action = report is None or report.deny is not FirewallState.ABSENT
    actions = [
        f"project '{coordinates.project_id}', zone '{coordinates.zone}', instance '{coordinates.instance_name}', "
        f"allow '{coordinates.allow_rule}', deny '{coordinates.deny_rule}'; expected provider IDs: "
        f"instance '{_expected_id(instance_ownership)}', allow '{_expected_id(allow_ownership)}', "
        f"deny '{_expected_id(deny_ownership)}'."
    ]
    if instance_needs_action:
        instance_state = InstanceState.INDETERMINATE if report is None else report.instance
        observed_id = None if report is None else report.instance_resource_id
        actions.append(
            _instance_guidance(
                coordinates,
                state=instance_state,
                expected_id=None if instance_ownership is None else instance_ownership.resource_id,
                observed_id=observed_id,
            )
        )
    for name, needed, state, expected_id, observed_id, role in (
        (
            coordinates.allow_rule,
            allow_needs_action,
            FirewallState.INDETERMINATE if report is None else report.allow,
            None if allow_ownership is None else allow_ownership.resource_id,
            None if report is None else report.allow_resource_id,
            "allow",
        ),
        (
            coordinates.deny_rule,
            deny_needs_action,
            FirewallState.INDETERMINATE if report is None else report.deny,
            None if deny_ownership is None else deny_ownership.resource_id,
            None if report is None else report.deny_resource_id,
            "deny",
        ),
    ):
        if not needed:
            continue
        actions.append(
            _firewall_guidance(
                coordinates,
                name=name,
                role=role,
                state=state,
                expected_id=expected_id,
                observed_id=observed_id,
                instance_absent=report is not None and report.instance is InstanceState.ABSENT,
            )
        )
    return "Manual cleanup: " + " ".join(actions)


def _expected_id(ownership: InstanceOwnership | FirewallOwnership | None) -> str:
    return "unknown" if ownership is None else ownership.resource_id


def _instance_guidance(
    coordinates: CleanupCoordinates,
    *,
    state: InstanceState,
    expected_id: str | None,
    observed_id: str | None,
) -> str:
    inspect = (
        f"Inspect `gcloud compute instances describe {coordinates.instance_name} "
        f"--project {coordinates.project_id} --zone {coordinates.zone} --format='value(id)'`"
    )
    identity = f"expected provider ID '{expected_id or 'unknown'}', observed '{observed_id or 'unknown'}'"
    if state is InstanceState.SURVIVING and expected_id is not None and observed_id == expected_id:
        return (
            f"Owned instance survivor ({identity}). {inspect}; only while it still reports provider ID "
            f"'{expected_id}', run `gcloud compute instances delete {coordinates.instance_name} "
            f"--project {coordinates.project_id} --zone {coordinates.zone}`."
        )
    if state is InstanceState.COLLISION:
        return f"Same-name instance collision ({identity}). {inspect}; do not delete by name; escalate ownership."
    return f"Instance ownership is unknown ({identity}). {inspect}; do not delete by name; escalate ownership."


def _firewall_guidance(
    coordinates: CleanupCoordinates,
    *,
    name: str,
    role: str,
    state: FirewallState,
    expected_id: str | None,
    observed_id: str | None,
    instance_absent: bool,
) -> str:
    inspect = (
        f"Inspect `gcloud compute firewall-rules describe {name} --project {coordinates.project_id} "
        "--format='value(id)'`"
    )
    identity = f"expected provider ID '{expected_id or 'unknown'}', observed '{observed_id or 'unknown'}'"
    if state is FirewallState.REALIZED and expected_id is not None and observed_id == expected_id:
        if role == "deny" and not instance_absent:
            return (
                f"Owned deny rule retained while instance absence is unproven ({identity}). {inspect}; keep it until "
                f"the owned instance is absent, then re-verify provider ID '{expected_id}' before running "
                f"`gcloud compute firewall-rules delete {name} --project {coordinates.project_id}`."
            )
        return (
            f"Owned {role} rule survivor ({identity}). {inspect}; only while it still reports provider ID "
            f"'{expected_id}', run `gcloud compute firewall-rules delete {name} "
            f"--project {coordinates.project_id}`."
        )
    if state is FirewallState.MISMATCHED:
        return (
            f"Same-name {role} rule collision or shape mismatch ({identity}). "
            f"{inspect}; do not delete by name; escalate ownership."
        )
    return (
        f"{role.capitalize()} rule ownership is unknown ({identity}). "
        f"{inspect}; do not delete by name; escalate ownership."
    )
