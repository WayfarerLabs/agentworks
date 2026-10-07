"""Private exact persisted managed-VM run admission."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.db import OperationResourceKind
from agentworks.errors import ValidationError
from agentworks.operations import OperationOwner

from ._helper_launcher import IdentityPlan, _validate_plan
from ._managed_job_protocol import encode_managed_job_fact
from ._managed_runs import (
    ManagedLaunchState,
    ManagedRunIdentity,
    ManagedRunLifetime,
    ManagedRunOwner,
    ManagedRunOwnerKind,
    ManagedRunReceipt,
    ManagedRunRecord,
    ManagedRunRepository,
    ManagedTargetIdentity,
    ManagedTargetKind,
)
from ._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS
from ._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from .carrier import Deadline

if TYPE_CHECKING:
    from ._execution_operation import ExecutionOperation


class ManagedDeadlineExpired(ValidationError):
    """A bound managed read was refused before borrowing because time elapsed."""


def preflight_bound_run(
    repository: ManagedRunRepository,
    identity: ManagedRunIdentity,
    *,
    target: ManagedTargetIdentity,
    guest: VMGuestIdentity,
    root_plan: IdentityPlan,
    runtime_selection: RuntimeSelection,
    deadline: Deadline,
    owner: OperationOwner,
    expected_resource_owner: ManagedRunOwner | None = None,
    execution_operation: ExecutionOperation | None = None,
) -> tuple[ManagedRunRecord, bytes]:
    """Inspect one exact row under explicit resource or retained operation custody."""
    if type(deadline) is not Deadline or deadline.expires_at is None:
        raise ValidationError("Managed access requires a finite deadline")
    if deadline.expired:
        raise ManagedDeadlineExpired("Managed access deadline expired before admission")
    if execution_operation is not None:
        if expected_resource_owner is not None:
            raise ValidationError("Managed operation access cannot use a resource-owner binding")
        run = execution_operation.require_managed_run(
            identity,
            repository=repository,
            owner=owner,
            target=target,
            guest=guest,
            root_plan=root_plan,
            runtime_selection=runtime_selection,
        )
        receipt = run.receipt
        record = repository.inspect(identity)
        if (
            record is None
            or record.identity != identity
            or record.spec != receipt.spec
            or run.reserved is None
            or record.output_policy != run.reserved.output_policy
            or record.launch_state is not ManagedLaunchState.RECEIPT_CONFIRMED
        ):
            raise ValidationError("Managed access requires its exact acknowledged operation reservation")
        return record, encode_managed_job_fact(receipt)
    if (
        type(expected_resource_owner) is not ManagedRunOwner
        or expected_resource_owner.kind is not ManagedRunOwnerKind.RESOURCE
    ):
        raise ValidationError("Independent managed access requires an explicit resource-owner binding")
    if (
        type(identity) is not ManagedRunIdentity
        or type(target) is not ManagedTargetIdentity
        or target.kind is not ManagedTargetKind.VM
        or type(guest) is not VMGuestIdentity
        or target.boot_id != vm_guest_boot_id(guest)
        or type(runtime_selection) is not RuntimeSelection
        or runtime_selection.target_os is not RuntimeTargetOS.LINUX
        or not isinstance(owner, OperationOwner)
        or owner.ownership.scope.resource_kind is not OperationResourceKind.VM
        or owner.ownership.scope.resource_name != target.name
    ):
        raise ValidationError("Managed access requires an owned exact Linux VM and finite deadline")
    if _validate_plan(root_plan).euid != 0:
        raise ValidationError("Managed access requires a root helper plan")

    record = repository.inspect(identity)
    if (
        record is None
        or record.identity != identity
        or record.spec.target != target
        or record.spec.target.kind is not ManagedTargetKind.VM
        or record.spec.lifetime is not ManagedRunLifetime.INDEPENDENT
        or record.spec.owner != expected_resource_owner
    ):
        raise ValidationError("Managed access requires an exact independent VM reservation")
    receipt = ManagedRunReceipt(record.identity, record.identity.unit_name, record.spec)
    return record, encode_managed_job_fact(receipt)
