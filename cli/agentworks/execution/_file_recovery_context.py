"""Exact versioned file identity on one caller-retained recovery action."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError
from agentworks.execution._runtime_prerequisite import _NumericGuestBootstrap
from agentworks.vms._recovery_vm_span import RecoveryVMPreparedContext, RecoveryVMSpan

if TYPE_CHECKING:
    from agentworks.db.operations import LifecycleObligation
    from agentworks.execution._file_obligation import FileCallObligation
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution.carrier import Carrier, Deadline
    from agentworks.operations import OperationOwner


def require_recovery_context(
    owner: OperationOwner,
    call: FileCallObligation,
    context: RecoveryVMPreparedContext | None,
    *,
    deadline: Deadline | None = None,
) -> tuple[IdentityPlan, _NumericGuestBootstrap | None]:
    """Compare complete authority, retaining historical delivery-relative modes."""
    bootstrap = call.bootstrap
    if bootstrap is None:
        if context is not None:
            raise StateError("Version-one recovery cannot infer a numeric bootstrap")
        return call.identity_plan, None
    if type(context) is not RecoveryVMPreparedContext or type(context._span) is not RecoveryVMSpan:  # noqa: SLF001
        raise StateError("Numeric file recovery requires a protected span action")
    context._span._require_prepared_action(context, owner, deadline)  # noqa: SLF001
    if (
        context.target != call.target
        or context.guest != bootstrap.guest
        or context.runtime_selection != call.runtime_selection
        or context.root_plan.expected != bootstrap.root_entry.expected
    ):
        raise StateError("Numeric file recovery fresh identity differs from its record")
    for plan in (context.ordinary_plan, context.root_plan):
        if plan.expected == call.identity_plan.expected:
            return plan, _NumericGuestBootstrap(context.root_plan, context.guest)
    raise StateError("Numeric file recovery body authority is unavailable")


def require_record_version(call: FileCallObligation, obligation: LifecycleObligation) -> None:
    if obligation.payload_version != call.payload_version:
        raise StateError("File recovery envelope differs from its encoded version")


def recovery_delivery(
    owner: OperationOwner,
    call: FileCallObligation,
    context: RecoveryVMPreparedContext | None,
    carrier: Carrier | None,
    deadline: Deadline,
) -> tuple[Carrier, IdentityPlan, _NumericGuestBootstrap | None]:
    if call.bootstrap is not None and carrier is not None:
        raise StateError("Numeric file recovery forbids caller carrier substitution")
    plan, bootstrap = require_recovery_context(owner, call, context, deadline=deadline)
    if bootstrap is not None:
        assert context is not None
        return context.carrier, plan, bootstrap
    if carrier is None:
        raise StateError("Version-one recovery requires its explicit carrier")
    return carrier, plan, None
