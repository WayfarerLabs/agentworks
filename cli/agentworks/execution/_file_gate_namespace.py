"""Owned namespace preparation retained before root setup and helper dispatch."""

from __future__ import annotations

import secrets
from dataclasses import replace
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution._file_effect_gate_exchange import (
    GateControlObservationState,
    exchange_file_effect_gate,
)
from agentworks.execution._file_effect_gate_protocol import GateControlOperation
from agentworks.execution._file_gate_namespace_exchange import NamespaceResult, exchange_namespace
from agentworks.execution._file_gate_namespace_obligation import (
    NAMESPACE_OBLIGATION_VERSION,
    NamespaceObligation,
    encode_namespace_obligation,
)
from agentworks.execution._file_gate_namespace_protocol import NamespaceStatus
from agentworks.execution._file_gate_setup import FileEffectGateSetup
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution.carrier import Deadline, Dispatch, ExitStatus

if TYPE_CHECKING:
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._managed_runs import ManagedTargetIdentity
    from agentworks.execution._runtime_prerequisite import RuntimeSelection, _NumericGuestBootstrap
    from agentworks.execution.carrier import Carrier
    from agentworks.operations import LifecycleObligation, OperationBorrow, OperationOwner


class NamespacePreparation:
    """One wrapper, borrow and durable row, including all escaping control paths."""

    def __init__(
        self,
        owner: OperationOwner,
        carrier: Carrier,
        target: ManagedTargetIdentity,
        agent_plan: IdentityPlan,
        bootstrap: _NumericGuestBootstrap,
        runtime_selection: RuntimeSelection,
        deadline: Deadline,
    ) -> None:
        if type(deadline) is not Deadline or deadline.expires_at is None or deadline.expired:
            raise ValidationError("Namespace preparation requires a live finite deadline")
        self._owner = owner
        self._carrier = carrier
        self._deadline = deadline
        self._record = NamespaceObligation(
            secrets.token_hex(16),
            agent_plan.expected.euid,
            agent_plan.expected.egid,
            target,
            bootstrap,
            runtime_selection,
            FileEffectGateSetup.for_target(target, 0, bootstrap.guest),
        )
        self._payload = encode_namespace_obligation(self._record)
        self._borrow: OperationBorrow | None = None
        self._operation: BorrowedFixedHelperCarrier | None = None
        self._obligation: LifecycleObligation | None = None
        self._obligation_id = secrets.token_hex(16)
        self._result: NamespaceResult | None = None
        self._unused = False
        self._started = False
        self.settled = False
        self.prepared = False

    def prepare(self) -> None:
        """Dispatch once, with pending custody before SETUP and binding before effects."""
        if self._started:
            raise StateError("Namespace preparation cannot be replayed")
        self._started = True
        self._borrow = self._owner.borrow()
        self._operation = BorrowedFixedHelperCarrier(self._carrier, self._borrow)
        self._obligation = self._borrow.install_dispatch_obligation(
            self._obligation_id,
            "file-gate-namespace",
            payload_version=NAMESPACE_OBLIGATION_VERSION,
            payload=self._payload,
        )
        record = self._record
        result = exchange_file_effect_gate(
            self._operation,
            operation=GateControlOperation.SETUP,
            path=record.setup.path,
            guest=record.bootstrap.guest,
            scope_name=record.target.name,
            plan=record.bootstrap.root_entry,
            deadline=self._deadline,
            runtime_selection=record.runtime_selection,
            bootstrap=record.bootstrap,
        )
        if result.dispatch is Dispatch.NOT_SENT:
            self._unused = True
            self.settle_local(self._deadline)
            raise StateError("Namespace root gate setup was not sent")
        normal = self._operation.settle(result.dispatch, result.carrier_completion)
        observation = result.observation
        if (
            not normal
            or result.runtime_prerequisite.state is not RuntimePrerequisiteState.READY
            or observation is None
            or observation.state is not GateControlObservationState.RESOLVED
            or observation.binding is None
        ):
            raise StateError("Namespace root gate setup remains uncertain")
        self._record = replace(record, binding=observation.binding)
        self._payload = encode_namespace_obligation(self._record)
        self._obligation.publish_payload(
            expected_revision=self._obligation.payload_revision,
            payload_version=NAMESPACE_OBLIGATION_VERSION,
            payload=self._payload,
        )
        if self._deadline.expired:
            raise StateError("Namespace preparation deadline expired")
        self._result = exchange_namespace(self._operation, self._record, self._deadline)
        self.settle_local(self._deadline)
        if not self.prepared:
            raise StateError("Managed UID namespace preparation is unavailable")

    def settle_local(self, deadline: Deadline) -> None:
        """Settle cached positive facts after local delivery closes, without replay."""
        if self.settled:
            return
        operation = self._operation
        borrow = self._borrow
        if operation is None or borrow is None:
            return
        result = self._result
        if not self._unused and (
            result is None
            or result.dispatch is not Dispatch.NOT_SENT
            and (
                result.dispatch is not Dispatch.SENT
                or result.completion != ExitStatus(code=0)
                or result.status not in (NamespaceStatus.PREPARED, NamespaceStatus.REFUSED)
            )
        ):
            return
        if not self._owner.close_local_delivery(deadline):
            return
        # Cached positive protocol and dispatch facts authorize only closure.
        # Reconcile this original attempt if its local settlement reply escaped.
        if operation.outstanding_attempt is not None:
            operation.settle_helper_closure()
        self.prepared = result is not None and result.status is NamespaceStatus.PREPARED
        if not borrow.closed:
            borrow.close()
        self.settled = True
