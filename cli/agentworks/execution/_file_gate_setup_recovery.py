"""Private reconciliation of one retained, setup-only file-call obligation."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import StateError
from agentworks.execution._file_effect_gate_exchange import (
    GateControlCandidateResult,
    GateControlObservationState,
    exchange_file_effect_gate,
)
from agentworks.execution._file_effect_gate_protocol import GateControlOperation
from agentworks.execution._file_obligation import (
    FileCallFamily,
    FileCallObligation,
    decode_file_call_obligation,
)
from agentworks.execution._file_recovery_context import (
    recovery_delivery,
    require_record_version,
    require_recovery_context,
)
from agentworks.execution._fixed_helper_operation import AttemptBoundHelperCarrier
from agentworks.execution._managed_runs import ManagedTargetIdentity
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution.carrier import Dispatch, ExitStatus

if TYPE_CHECKING:
    from agentworks.db.operations import LifecycleObligation
    from agentworks.execution.carrier import Carrier, Deadline
    from agentworks.operations import OperationOwner, RecoveredLifecycleObligation
    from agentworks.vms._recovery_vm_span import RecoveryVMPreparedContext


@dataclass(slots=True, repr=False)
class FileGateSetupRecovery:
    """Inspect and settle a still-setup-only file call after takeover.

    A positive exact inspection proves the gate's creation completed. The
    predecessor cannot publish a binding after database generation takeover,
    so this path cannot have started snapshot or upload work. It does not resolve other
    obligations or release the whole operation owner.
    """

    _owner: OperationOwner
    _persisted: LifecycleObligation
    _call: FileCallObligation
    _bound: RecoveredLifecycleObligation
    _context: RecoveryVMPreparedContext | None = None

    @classmethod
    def open(
        cls,
        owner: OperationOwner,
        target: ManagedTargetIdentity,
        obligation: LifecycleObligation,
        *,
        context: RecoveryVMPreparedContext | None = None,
    ) -> FileGateSetupRecovery:
        """Bind the exact current setup-only row under recovery ownership."""
        scope = owner.ownership.scope
        try:
            call = decode_file_call_obligation(obligation.payload)
        except ValueError:
            raise StateError(
                "file gate setup recovery obligation payload is invalid",
                entity_kind=scope.resource_kind,
                entity_name=scope.resource_name,
            ) from None
        require_record_version(call, obligation)
        require_recovery_context(owner, call, context)
        if (
            obligation.ownership != owner.ownership
            or type(target) is not ManagedTargetIdentity
            or target.kind.value != scope.resource_kind.value
            or target.name != scope.resource_name
            or call.family not in {FileCallFamily.DOWNLOAD, FileCallFamily.UPLOAD, FileCallFamily.PACKAGE_UPLOAD}
            or call.target != target
            or call.gate_setup is None
            or call.effect_gate is not None
        ):
            raise StateError(
                "file gate setup recovery requires the current setup-only obligation",
                entity_kind=scope.resource_kind,
                entity_name=scope.resource_name,
            )
        bound = owner.rebind_possible_effect_lifecycle_obligation(
            obligation.obligation_id,
            "file-call",
            payload_version=call.payload_version,
            payload=obligation.payload,
            payload_revision=obligation.payload_revision,
        )
        return cls(owner, obligation, call, bound, context)

    def inspect(self, carrier: Carrier | None = None, *, deadline: Deadline) -> GateControlCandidateResult:
        """Resolve this setup row only after a complete positive INSPECT."""
        delivery, plan, bootstrap = recovery_delivery(self._owner, self._call, self._context, carrier, deadline)
        setup = self._call.gate_setup
        assert setup is not None
        dispatch = self._bound.open_dispatch()
        attempt = None
        try:
            attempt = dispatch.begin_attempt()
            result = exchange_file_effect_gate(
                AttemptBoundHelperCarrier(delivery, attempt),
                operation=GateControlOperation.INSPECT,
                path=setup.path,
                guest=setup.guest,
                scope_name=self._call.target.name,
                plan=plan,
                deadline=deadline,
                runtime_selection=self._call.runtime_selection,
                bootstrap=bootstrap,
            )
            terminated = result.dispatch is Dispatch.NOT_SENT or (
                result.dispatch is Dispatch.SENT and result.carrier_completion == ExitStatus(code=0)
            )
            if not terminated or not attempt.local_delivery.settled:
                dispatch.handoff_unresolved()
                return result
            attempt.settle()
            dispatch.close()
        except BaseException:
            with suppress(BaseException):
                if attempt is None:
                    dispatch._abort_unreturned_attempt()  # noqa: SLF001
                else:
                    try:
                        dispatch.handoff_unresolved()
                    except StateError:
                        dispatch._abort_unreturned_attempt()  # noqa: SLF001
            raise
        observation = result.observation
        if (
            result.dispatch is Dispatch.SENT
            and result.carrier_failure is None
            and result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
            and observation is not None
            and observation.state is GateControlObservationState.RESOLVED
            and observation.binding is not None
        ):
            self._owner.rebind_lifecycle_obligation(
                self._persisted.obligation_id,
                "file-call",
                payload_version=self._persisted.payload_version,
                payload=self._persisted.payload,
            ).resolve()
        return result
