"""What the operator is told when GCE cleanup cannot prove a resource gone, and the interrupt protocol around it.

Guidance names each resource's exact identity and allows a delete by name
only while the resource reports the provider ID its own insert recorded.
Every value placed in a suggested command is shell-quoted.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NoReturn

from agentworks import output
from agentworks.plugins.gcp.cleanup import CleanupTarget, InstanceState, RollbackReport
from agentworks.plugins.gcp.network import FirewallState

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.plugins.gcp.cleanup import InstanceOwnership, UnresolvedInsert
    from agentworks.plugins.gcp.network import FirewallOwnership


@dataclass(frozen=True)
class GcloudResource:
    """One GCE resource as the gcloud commands that inspect, wait for and delete it address it.

    ``zone`` is the instance's zone; a firewall rule is global.
    """

    kind: Literal["instance", "firewall rule"]
    project_id: str
    name: str
    zone: str | None = None

    def _scope(self, zone_flag: str) -> str:
        """Where this resource's operations are: ``zone_flag`` and the zone, or ``--global`` for a firewall rule."""
        return "--global" if self.zone is None else f"{zone_flag} {shlex.quote(self.zone)}"

    def _command(self, verb: str) -> str:
        group = "instances" if self.kind == "instance" else "firewall-rules"
        location = "" if self.zone is None else f" {self._scope('--zone')}"
        return (
            f"gcloud compute {group} {verb} {shlex.quote(self.name)} --project {shlex.quote(self.project_id)}{location}"
        )

    def describe(self) -> str:
        return f"`{self._command('describe')} --format='value(id)'`"

    def delete(self) -> str:
        return f"`{self._command('delete')}`"

    def insert_wait(self, operation_name: str | None) -> str:
        """How to watch this resource's insert until it can no longer create the resource."""
        project = shlex.quote(self.project_id)
        if operation_name is not None:
            return (
                f"`gcloud compute operations describe {shlex.quote(operation_name)} --project {project} "
                f"{self._scope('--zone')}` until it reports DONE"
            )
        collection = "instances" if self.kind == "instance" else "firewalls"
        query = shlex.quote(f"operationType=insert AND targetLink~/{collection}/{self.name}$")
        return (
            f"`gcloud compute operations list --project {project} {self._scope('--zones')} --filter={query}` "
            "until none it lists is PENDING or RUNNING"
        )


def may_still_appear(resource: GcloudResource, *, operation_name: str | None, expected_id: str | None) -> str:
    """The steps for a resource an insert whose outcome is unknown may still create.

    Wait for the insert, then delete the resource only while it reports the
    provider ID that insert's operation named; with none known, escalate.
    """
    then = (
        f"only while it reports provider ID '{expected_id}', run {resource.delete()}."
        if expected_id is not None
        else "do not delete it by name; escalate ownership."
    )
    return f"Watch {resource.insert_wait(operation_name)}, then inspect {resource.describe()}; if it appears, {then}"


def manual_cleanup_guidance(target: CleanupTarget, report: RollbackReport | None) -> str:
    """Provider-ID-gated recovery guidance for ``report``, or, with no report, for every resource as unknown.

    Each resource that may still appear gets the steps for its insert: with
    no report, each whose insert was unresolved when cleanup began.
    """
    coordinates = target.coordinates
    header = (
        f"project '{coordinates.project_id}', zone '{coordinates.zone}', instance '{coordinates.instance_name}', "
        f"allow '{coordinates.allow_rule}', deny '{coordinates.deny_rule}'; expected provider IDs: "
        f"instance '{_owned_id(target.instance_ownership) or 'unknown'}', "
        f"allow '{_owned_id(target.allow_ownership) or 'unknown'}', "
        f"deny '{_owned_id(target.deny_ownership) or 'unknown'}'"
    )
    if report is None:
        report = RollbackReport(
            InstanceState.INDETERMINATE,
            FirewallState.INDETERMINATE,
            FirewallState.INDETERMINATE,
            may_appear=target.unresolved,
        )
    actions = [header + "."]
    may_appear = report.may_appear
    instance = GcloudResource("instance", coordinates.project_id, coordinates.instance_name, coordinates.zone)
    actions.extend(
        resource_guidance(
            subject="Instance",
            resource=instance,
            state=_instance_state_guidance(
                instance,
                report.instance,
                expected_id=_owned_id(target.instance_ownership),
                observed_id=report.instance_resource_id,
            ),
            uncertain=report.instance is InstanceState.INDETERMINATE,
            observed_id=report.instance_resource_id,
            insert=may_appear.instance,
            expected_id=_owned_id(target.instance_ownership),
        )
    )
    instance_gone = report.instance is InstanceState.ABSENT and may_appear.instance is None
    for role, name, state, observed_id, ownership, insert in (
        (
            "allow",
            coordinates.allow_rule,
            report.allow,
            report.allow_resource_id,
            target.allow_ownership,
            may_appear.allow,
        ),
        ("deny", coordinates.deny_rule, report.deny, report.deny_resource_id, target.deny_ownership, may_appear.deny),
    ):
        rule = GcloudResource("firewall rule", coordinates.project_id, name)
        actions.extend(
            resource_guidance(
                subject=role.capitalize(),
                resource=rule,
                state=_firewall_state_guidance(
                    rule,
                    role=role,
                    state=state,
                    expected_id=_owned_id(ownership),
                    observed_id=observed_id,
                    instance_gone=instance_gone,
                ),
                uncertain=state is FirewallState.INDETERMINATE,
                observed_id=observed_id,
                insert=insert,
                expected_id=_owned_id(ownership),
            )
        )
    return "Manual cleanup: " + " ".join(actions)


def rollback_then_raise(primary: Exception, rollback: Callable[[], RollbackReport], target: CleanupTarget) -> NoReturn:
    """Run bounded cleanup without replacing an ordinary primary failure."""
    report: RollbackReport | None = None
    cleanup_failed = False
    first_interrupt: KeyboardInterrupt | None = None
    try:
        report = rollback()
    except KeyboardInterrupt as interrupt:
        first_interrupt = interrupt
    except Exception:
        cleanup_failed = True
    if first_interrupt is not None:
        first_interrupt.__cause__ = None
        first_interrupt.__context__ = None
        rollback_after_interrupt(first_interrupt, rollback, target)
    if cleanup_failed:
        output.warn("GCE rollback failed unexpectedly. " + manual_cleanup_guidance(target, None))
    if report is not None and not report.clean:
        output.warn(manual_cleanup_guidance(target, report))
    primary.__cause__ = None
    primary.__context__ = None
    raise primary


def rollback_after_interrupt(
    primary: KeyboardInterrupt, rollback: Callable[[], RollbackReport], target: CleanupTarget
) -> NoReturn:
    """Clean after the first interrupt; abandon promptly after the second."""
    output.warn(
        f"Interrupted: cleaning up partial GCE resources for '{target.coordinates.instance_name}', "
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
        output.warn("Cleanup abandoned. " + manual_cleanup_guidance(target, None))
    elif report is None:
        output.warn("GCE cleanup failed unexpectedly. " + manual_cleanup_guidance(target, None))
    elif not report.clean:
        output.warn(manual_cleanup_guidance(target, report))
    primary.__cause__ = None
    primary.__context__ = None
    raise primary


def _owned_id(ownership: InstanceOwnership | FirewallOwnership | None) -> str | None:
    return None if ownership is None else ownership.resource_id


def resource_guidance(
    *,
    subject: str,
    resource: GcloudResource,
    state: str | None,
    uncertain: bool,
    observed_id: str | None,
    insert: UnresolvedInsert | None,
    expected_id: str | None,
) -> list[str]:
    """The sentences one resource needs: its read's ``state``, and its unresolved insert's steps led by ``subject``.

    Steps for an insert that may still create the resource cover a read of
    it whose ownership is unknown (``uncertain``) too, so those replace that
    read's sentence and carry the provider ID it observed.
    """
    if insert is None:
        return [] if state is None else [state]
    sentences = [] if state is None or uncertain else [state]
    sentences.append(
        f"{subject} insert outcome unknown, so it may still appear "
        f"({_insert_identity(insert, expected_id=expected_id, observed_id=observed_id if uncertain else None)}). "
        + may_still_appear(resource, operation_name=insert.operation_name, expected_id=expected_id)
    )
    return sentences


def _insert_identity(insert: UnresolvedInsert, *, expected_id: str | None, observed_id: str | None) -> str:
    """What an unresolved insert's steps name: the expected and any observed provider ID, and its operation."""
    observed = f", observed '{observed_id}'" if observed_id is not None else ""
    return (
        f"expected provider ID '{expected_id or 'unknown'}'{observed}, operation '{insert.operation_name or 'unknown'}'"
    )


def _instance_state_guidance(
    instance: GcloudResource,
    state: InstanceState,
    *,
    expected_id: str | None,
    observed_id: str | None,
) -> str | None:
    if state is InstanceState.ABSENT:
        return None
    inspect = f"Inspect {instance.describe()}"
    identity = f"expected provider ID '{expected_id or 'unknown'}', observed '{observed_id or 'unknown'}'"
    if state is InstanceState.SURVIVING and expected_id is not None and observed_id == expected_id:
        return (
            f"Owned instance survivor ({identity}). {inspect}; only while it still reports provider ID "
            f"'{expected_id}', run {instance.delete()}."
        )
    if state is InstanceState.COLLISION:
        return f"Same-name instance collision ({identity}). {inspect}; do not delete by name; escalate ownership."
    return f"Instance ownership is unknown ({identity}). {inspect}; do not delete by name; escalate ownership."


def _firewall_state_guidance(
    rule: GcloudResource,
    *,
    role: str,
    state: FirewallState,
    expected_id: str | None,
    observed_id: str | None,
    instance_gone: bool,
) -> str | None:
    if state is FirewallState.ABSENT:
        return None
    inspect = f"Inspect {rule.describe()}"
    identity = f"expected provider ID '{expected_id or 'unknown'}', observed '{observed_id or 'unknown'}'"
    if state is FirewallState.REALIZED and expected_id is not None and observed_id == expected_id:
        if role == "deny" and not instance_gone:
            return (
                f"Owned deny rule retained while instance absence is unproven ({identity}). {inspect}; keep it until "
                "the instance describe reports not found and no instance insert operation is still running, then "
                f"re-verify provider ID '{expected_id}' before running {rule.delete()}."
            )
        return (
            f"Owned {role} rule survivor ({identity}). {inspect}; only while it still reports provider ID "
            f"'{expected_id}', run {rule.delete()}."
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
