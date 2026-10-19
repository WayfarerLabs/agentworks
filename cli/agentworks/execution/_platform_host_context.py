"""Private ordinary platform-host context and same-process cleanup custody."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from threading import Lock
from typing import TYPE_CHECKING

from agentworks.capabilities.base import RunContext, TargetAbsence
from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import StateError, ValidationError
from agentworks.operations import OperationOwner

from ._account_protocol import AccountRequest, AccountRequestError, encode_account_request
from ._execution_operation import ExecutionOperation
from ._file_operation import FileOperation
from ._file_paths import normalized_root
from ._runtime_prerequisite import RuntimeSelection
from ._target_identity import (
    TargetIdentityControlFact,
    TargetIdentityPreparation,
    TargetIdentityStatus,
    prepare_target_identity,
)
from .access import ExecutionAccess, FileAccess
from .carrier import Deadline
from .target import ExecutionIdentity, ExecutionRoute, ExecutionTarget

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .carrier import Carrier


class PlatformHostContextControlFact(Exception):
    """Retain the exact private lifetime for a bounded same-process cleanup retry."""

    def __init__(self, workflow: _HostLifetime) -> None:
        self._workflow = workflow
        super().__init__("Platform-host context retains cleanup custody")

    def retry_cleanup(self, deadline: Deadline) -> None:
        """Retry only closeout, without preparing or reopening the body."""
        if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
            raise ValidationError("Platform-host cleanup requires a live finite deadline")
        self._workflow.close(deadline)


@dataclass(slots=True, repr=False)
class _HostLifetime:
    owner: OperationOwner
    deadline: Deadline
    identity: TargetIdentityPreparation | None = None
    files: FileOperation | None = None
    execution: ExecutionOperation | None = None
    body_admission_closed: bool = False
    finalizing: bool = False
    _cleanup_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def selected_deadline(self) -> Deadline:
        if self.body_admission_closed:
            raise StateError("Platform-host context no longer admits body work")
        return self.deadline

    def _components_settled(self) -> bool:
        identity = self.identity
        return not (
            (
                identity is not None
                and (
                    identity.pending_remote_effects
                    or identity.coordination_uncertain
                    or identity.requires_owner_retention
                )
            )
            or (self.files is not None and self.files.has_pending_custody)
            or (self.execution is not None and self.execution.has_pending_custody)
        )

    def close(self, deadline: Deadline) -> None:
        """Stop body admission before observing or settling any retained work."""
        self.body_admission_closed = True
        self.owner.stop_admission()
        if not self._cleanup_lock.acquire(blocking=False):
            raise StateError("Platform-host cleanup is already active")
        try:
            self._close(deadline)
        finally:
            self._cleanup_lock.release()

    def _close(self, deadline: Deadline) -> None:
        if self.finalizing:
            # Release may have committed before interruption. Reconcile that
            # exact owner transition without inspecting an already removed claim.
            self.owner.close()
            return
        self.owner.retire_unused_borrow()
        execution = self.execution
        if execution is not None and execution.closing_scratch_helper_pending:
            execution.finish_scratch_cleanup(deadline)
        # Delivery closure alone says nothing about remote helper termination.
        if not self.owner.close_local_delivery(deadline):
            raise StateError("Platform-host context retains unsettled helper delivery")
        if execution is not None and execution.active_inline_calls:
            execution.observe_inline_cleanup(deadline)
        # Completed file calls can still hold the serial borrow. Retire their
        # exact bookkeeping before resolving the execution lifetime row.
        if self.files is not None:
            self.files.settle_local_file_bookkeeping(deadline)
            self.files.settle_download_bookkeeping()
            self.files.settle_completed_bookkeeping()
        if execution is not None:
            try:
                execution.finish()
            except BaseException as control:
                try:
                    execution.finish_scratch_cleanup(deadline)
                except BaseException:
                    raise control from control.__cause__
                raise
            execution.finish_scratch_cleanup(deadline)
        if not self.owner.close_local_delivery(deadline):
            raise StateError("Platform-host context retains unsettled local delivery")
        if not self._components_settled() or self.owner.list_pending_lifecycle_obligations():
            raise StateError("Platform-host context retains unsettled work")
        self.owner.seal_lifecycle_obligations()
        self.owner.record_effects_resolved()
        self.finalizing = True
        self.owner.close()


def _prepare_context(
    lifetime: _HostLifetime,
    ctx: RunContext,
    carrier: Carrier,
    *,
    delivery_account: str,
    workload_account: str,
    include_elevated: bool,
    runtime_selection: RuntimeSelection,
    trusted_root: PurePosixPath,
) -> RunContext:
    try:
        identity = prepare_target_identity(
            carrier,
            delivery_account=delivery_account,
            workload_account=workload_account,
            include_elevated=include_elevated,
            runtime_selection=runtime_selection,
            deadline=lifetime.deadline,
            owner=lifetime.owner,
        )
    except BaseException as control:
        if isinstance(control.__cause__, TargetIdentityControlFact):
            lifetime.identity = control.__cause__.preparation
        raise
    lifetime.identity = identity
    if (
        lifetime.deadline.expired
        or identity.status is not TargetIdentityStatus.PREPARED
        or identity.ordinary_plan is None
        or (include_elevated and identity.elevated_plan is None)
    ):
        raise StateError("Platform-host account preparation is unavailable")
    lifetime.files = FileOperation(lifetime.owner, lifetime.owner.ownership.scope)
    lifetime.execution = ExecutionOperation(lifetime.owner, None)
    files = FileAccess(
        lifetime.files,
        carrier,
        trusted_root=trusted_root,
        runtime_selection=runtime_selection,
        ordinary_plan=identity.ordinary_plan,
        elevated_plan=identity.elevated_plan,
        entity_kind="platform-host",
        entity_name="selected-host",
        deadline=lifetime.selected_deadline,
    )
    execution = ExecutionAccess(
        lifetime.execution,
        carrier,
        runtime_selection=runtime_selection,
        ordinary_plan=identity.ordinary_plan,
        elevated_plan=identity.elevated_plan,
        entity_kind="platform-host",
        entity_name="selected-host",
        deadline=lifetime.selected_deadline,
    )
    expected = identity.ordinary_plan.expected
    target = ExecutionTarget(
        execution,
        files,
        carrier.features,
        ExecutionIdentity(workload_account, expected.euid, expected.egid, expected.groups),
        ExecutionRoute.PLATFORM_HOST,
    )
    return RunContext(
        config=ctx.config,
        operation_scope=ctx.operation_scope,
        admin_target=ctx.admin_target(),
        agent_target=ctx.agent_target(),
        admin_execution_target=ctx.admin_execution_target(),
        agent_execution_target=ctx.agent_execution_target(),
        admin_execution_target_absence=ctx.admin_execution_target_absence() or TargetAbsence.UNAVAILABLE,
        agent_execution_target_absence=ctx.agent_execution_target_absence() or TargetAbsence.UNAVAILABLE,
        platform_host_execution_target=target,
        # Core stage composition preserves the exact scoped reader.
        secrets=ctx._secrets,  # noqa: SLF001
    )


@contextmanager
def platform_host_execution_context(
    db: Database,
    ctx: RunContext,
    scope: OperationScope,
    carrier: Carrier,
    *,
    delivery_account: str,
    workload_account: str,
    include_elevated: bool,
    runtime_selection: RuntimeSelection,
    trusted_root: PurePosixPath,
    deadline: Deadline,
) -> Iterator[RunContext]:
    """Bind ordinary host views from explicit core selection, claiming before discovery."""
    if type(db) is not Database or type(ctx) is not RunContext:
        raise ValidationError("Platform-host composition requires core database and context values")
    if type(scope) is not OperationScope or scope.resource_kind is not OperationResourceKind.PLATFORM_HOST:
        raise ValidationError("Platform-host composition requires an explicit platform-host scope")
    if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
        raise ValidationError("Platform-host composition requires a live finite deadline")
    if type(runtime_selection) is not RuntimeSelection or type(include_elevated) is not bool:
        raise ValidationError("Platform-host composition requires explicit runtime and elevation choices")
    if type(trusted_root) is not PurePosixPath or not normalized_root(str(trusted_root)):
        raise ValidationError("Platform-host composition requires a trusted normalized file root")
    for account in (delivery_account, workload_account):
        try:
            encode_account_request(AccountRequest("0" * 32, account))
        except AccountRequestError as error:
            raise ValidationError("Platform-host composition requires valid account names") from error
    owner = OperationOwner.acquire(db.operations, scope, "platform-host-execution-context")
    lifetime = _HostLifetime(owner, deadline)
    primary: BaseException | None = None
    try:
        yield _prepare_context(
            lifetime,
            ctx,
            carrier,
            delivery_account=delivery_account,
            workload_account=workload_account,
            include_elevated=include_elevated,
            runtime_selection=runtime_selection,
            trusted_root=trusted_root,
        )
    except BaseException as control:
        primary = control
        raise
    finally:
        try:
            lifetime.close(deadline)
        except BaseException as cleanup:
            fact = PlatformHostContextControlFact(lifetime)
            if primary is None:
                fact.__cause__ = cleanup.__cause__
                raise cleanup from fact
            primary.add_note("Platform-host teardown retained unresolved custody")
            fact.__cause__ = primary.__cause__
            primary.__cause__ = fact
            primary.__suppress_context__ = True
