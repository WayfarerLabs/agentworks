"""Fence the retained package child after process-loss takeover.

This adapter advances only its durable file-effect gate. It neither replays
the child nor settles its lifecycle or application checkpoint.
"""

from __future__ import annotations

import secrets
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import StateError
from agentworks.execution._file_effect_gate_exchange import (
    GateControlCandidateResult,
    GateControlObservationState,
    exchange_file_effect_gate,
)
from agentworks.execution._file_effect_gate_protocol import GateControlOperation
from agentworks.execution._file_obligation import (
    FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    FileCallFamily,
    FileCallObligation,
    decode_file_call_obligation,
    encode_file_call_obligation,
)
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState, RuntimeTargetOS
from agentworks.execution.carrier import Dispatch, ExitStatus
from agentworks.operations import RecoveredLifecycleObligation

if TYPE_CHECKING:
    from agentworks.db.operations import LifecycleObligation
    from agentworks.execution.carrier import Carrier, Deadline
    from agentworks.operations import OperationOwner


@dataclass(slots=True, repr=False)
class FilePackageFenceRecovery:
    """One exact current package child, retained through gate advancement."""

    _owner: OperationOwner
    _persisted: LifecycleObligation
    _call: FileCallObligation
    _bound: RecoveredLifecycleObligation
    _pending_publication: tuple[FileCallObligation, bytes] | None = None
    _fenced: bool = False

    @classmethod
    def open(
        cls,
        owner: OperationOwner,
        target: ManagedTargetIdentity,
        obligation: LifecycleObligation,
    ) -> FilePackageFenceRecovery:
        """Bind only an exact possible-effect package row with a durable gate."""
        scope = owner.ownership.scope
        try:
            call = decode_file_call_obligation(obligation.payload)
        except ValueError:
            raise StateError("package fence obligation payload is invalid") from None
        if (
            obligation.ownership.scope != scope
            or obligation.obligation_kind != "file-call"
            or obligation.payload_version != FILE_CALL_OBLIGATION_PAYLOAD_VERSION
            or obligation.state is not LifecycleObligationState.POSSIBLE_EFFECT
            or type(target) is not ManagedTargetIdentity
            or target.kind is not ManagedTargetKind.VM
            or target.kind.value != scope.resource_kind.value
            or target.name != scope.resource_name
            or call.target != target
            or call.family is not FileCallFamily.PACKAGE_UPLOAD
            or call.effect_gate is None
            or call.gate_setup is not None
            or call.runtime_selection.target_os is not RuntimeTargetOS.LINUX
        ):
            raise StateError("package fence requires the current bound package child")
        bound = owner.rebind_possible_effect_lifecycle_obligation(
            obligation.obligation_id,
            "file-call",
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=obligation.payload,
            payload_revision=obligation.payload_revision,
        )
        return cls(owner, obligation, call, bound)

    def advance(self, carrier: Carrier, *, deadline: Deadline) -> GateControlCandidateResult:
        """Fence old guest effects; retain the package child for separate recovery."""
        self._ensure_proposal()
        gate = self._call.effect_gate
        assert gate is not None and gate.proposed_generation is not None
        dispatch = self._bound.open_dispatch()
        attempt = None
        try:
            attempt = dispatch.begin_attempt()
            result = exchange_file_effect_gate(
                carrier,
                operation=GateControlOperation.ADVANCE,
                path=gate.path,
                guest=gate.guest,
                scope_name=self._call.target.name,
                plan=self._call.identity_plan,
                deadline=deadline,
                runtime_selection=self._call.runtime_selection,
                binding=gate,
            )
            terminated = result.dispatch is Dispatch.NOT_SENT or (
                result.dispatch is Dispatch.SENT and result.carrier_completion == ExitStatus(code=0)
            )
            if not terminated:
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
            and observation.binding == replace(gate, generation=gate.proposed_generation, proposed_generation=None)
        ):
            self._publish(replace(self._call, effect_gate=observation.binding))
        return result

    def _ensure_proposal(self) -> None:
        if self._fenced:
            raise StateError("package child fence is already confirmed by this recovery")
        if self._pending_publication is not None:
            call, payload = self._pending_publication
            if self._try_adopt(call, payload):
                if self._fenced:
                    raise StateError("package child fence is already confirmed by this recovery")
                return
            self._publish(call, payload=payload)
            if self._fenced:
                raise StateError("package child fence is already confirmed by this recovery")
            return
        gate = self._call.effect_gate
        assert gate is not None
        if gate.proposed_generation is None:
            self._publish(replace(self._call, effect_gate=replace(gate, proposed_generation=secrets.token_bytes(16))))

    def _publish(self, call: FileCallObligation, *, payload: bytes | None = None) -> None:
        payload = encode_file_call_obligation(call) if payload is None else payload
        self._pending_publication = (call, payload)
        bound = self._owner.rebind_lifecycle_obligation(
            self._persisted.obligation_id,
            "file-call",
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=self._persisted.payload,
        )
        try:
            persisted = bound.publish_payload(
                expected_revision=self._persisted.payload_revision,
                payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                payload=payload,
            )
        except BaseException:
            with suppress(BaseException):
                self._try_adopt(call, payload)
            raise
        self._adopt(call, payload, persisted)

    def _try_adopt(self, call: FileCallObligation, payload: bytes) -> bool:
        try:
            rebound = self._owner.rebind_lifecycle_obligation(
                self._persisted.obligation_id,
                "file-call",
                payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                payload=payload,
            )
        except StateError:
            return False
        self._adopt(call, payload, rebound._persisted_obligation)
        return True

    def _adopt(self, call: FileCallObligation, payload: bytes, persisted: LifecycleObligation) -> None:
        if (
            persisted.ownership != self._owner.ownership
            or persisted.obligation_id != self._persisted.obligation_id
            or persisted.obligation_kind != "file-call"
            or persisted.state is not LifecycleObligationState.POSSIBLE_EFFECT
            or persisted.payload_version != FILE_CALL_OBLIGATION_PAYLOAD_VERSION
            or persisted.payload != payload
            or persisted.payload_revision != self._persisted.payload_revision + (payload != self._persisted.payload)
        ):
            raise StateError("package fence publication did not advance exactly once")
        self._persisted = persisted
        self._call = call
        self._bound = RecoveredLifecycleObligation(self._owner, persisted)
        self._pending_publication = None
        self._fenced = call.effect_gate is not None and call.effect_gate.proposed_generation is None
