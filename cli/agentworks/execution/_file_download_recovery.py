"""Private recovery actions for one retained DOWNLOAD lifecycle effect.

This module deliberately exposes only snapshot reconciliation and exact
cleanup.  It never imports snapshot creation or chunking, and it does not
resolve the durable lifecycle obligation.  Adapter-specific drain evidence
is accepted only through this private boundary.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agentworks.db.operations import LifecycleObligationState
from agentworks.errors import StateError
from agentworks.execution._file_obligation import (
    FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    FileCallFamily,
    FileCallObligation,
    decode_file_call_obligation,
    encode_file_call_obligation,
)
from agentworks.execution._file_snapshot_exchange import (
    FileSnapshotCandidateResult,
    snapshot_cleanup,
    snapshot_reconcile,
)
from agentworks.execution.carrier import Dispatch, ExitStatus
from agentworks.operations import RecoveredLifecycleObligation

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db.operations import LifecycleObligation, OperationOwnership
    from agentworks.execution._managed_runs import ManagedTargetIdentity
    from agentworks.execution._scratch_receipt import ScratchCleanupDebt
    from agentworks.execution.carrier import Carrier, Deadline
    from agentworks.operations import OperationOwner


@dataclass(frozen=True, slots=True, repr=False)
class _DownloadDrainEvidence:
    """Private adapter evidence for one exact retained DOWNLOAD record.

    Its producer is adapter-specific.  This local-only module consumes the
    exact durable binding but does not establish remote helper drain itself.
    """

    ownership: OperationOwnership
    obligation_id: str
    payload_revision: int
    payload: bytes

    def _require_exact(
        self,
        ownership: OperationOwnership,
        obligation: LifecycleObligation,
        call: FileCallObligation,
        target: ManagedTargetIdentity,
    ) -> None:
        if (
            self.ownership != ownership
            or self.obligation_id != obligation.obligation_id
            or self.payload_revision != obligation.payload_revision
            or self.payload != obligation.payload
            or call.family is not FileCallFamily.DOWNLOAD
            or call.target != target
            or call.token is None
        ):
            raise StateError(
                "download recovery drain evidence does not match the retained obligation",
                entity_kind=ownership.scope.resource_kind,
                entity_name=ownership.scope.resource_name,
            )


@dataclass(slots=True, repr=False)
class FileDownloadRecovery:
    """One exact DOWNLOAD recovery binding with no generic replay surface."""

    _owner: OperationOwner
    _persisted: LifecycleObligation
    _call: FileCallObligation
    _bound: RecoveredLifecycleObligation
    _pending_cleanup_debt: tuple[FileCallObligation, bytes] | None = None

    @classmethod
    def open(
        cls,
        owner: OperationOwner,
        target: ManagedTargetIdentity,
        obligation: LifecycleObligation,
        evidence: _DownloadDrainEvidence,
    ) -> FileDownloadRecovery:
        """Bind one exactly retained DOWNLOAD record after adapter drain proof."""
        try:
            call = decode_file_call_obligation(obligation.payload)
        except ValueError:
            raise StateError(
                "download recovery obligation payload is invalid",
                entity_kind=owner.ownership.scope.resource_kind,
                entity_name=owner.ownership.scope.resource_name,
            ) from None
        evidence._require_exact(owner.ownership, obligation, call, target)
        bound = owner.rebind_possible_effect_lifecycle_obligation(
            obligation.obligation_id,
            "file-call",
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=obligation.payload,
            payload_revision=obligation.payload_revision,
        )
        return cls(owner, obligation, call, bound)

    def reconcile(self, carrier: Carrier, *, deadline: Deadline) -> FileSnapshotCandidateResult:
        """Observe retained snapshot ownership without snapshot creation."""
        self._reconcile_pending_cleanup_debt()
        token = self._call.token
        assert token is not None
        result = self._dispatch(
            lambda: snapshot_reconcile(
                carrier,
                token=token,
                plan=self._call.identity_plan,
                deadline=deadline,
                runtime_selection=self._call.runtime_selection,
            )
        )
        observation = result.observation
        debt = None if observation is None else observation.cleanup_debt
        if debt is None and observation is not None and observation.failure is not None:
            debt = observation.failure.cleanup_debt
        if debt is not None:
            self._persist_cleanup_debt(debt)
        return result

    def cleanup(self, carrier: Carrier, *, deadline: Deadline) -> FileSnapshotCandidateResult:
        """Attempt exact cleanup only after reconciliation persisted its debt."""
        self._reconcile_pending_cleanup_debt()
        debt = self._call.scratch_cleanup_debt
        if debt is None:
            raise StateError(
                "download recovery has no persisted snapshot cleanup debt",
                entity_kind=self._owner.ownership.scope.resource_kind,
                entity_name=self._owner.ownership.scope.resource_name,
            )
        token = self._call.token
        assert token is not None
        return self._dispatch(
            lambda: snapshot_cleanup(
                carrier,
                token=token,
                cleanup_debt=debt,
                plan=self._call.identity_plan,
                deadline=deadline,
                runtime_selection=self._call.runtime_selection,
            )
        )

    def _dispatch(self, action: Callable[[], FileSnapshotCandidateResult]) -> FileSnapshotCandidateResult:
        self._reconcile_pending_cleanup_debt()
        dispatch = self._bound.open_dispatch()
        try:
            attempt = dispatch.begin_attempt()
        except BaseException:
            with suppress(BaseException):
                dispatch.close()
            raise
        try:
            result = action()
        except BaseException:
            dispatch.handoff_unresolved()
            raise
        if _terminated_without_possible_effect(result.dispatch, result.carrier_completion):
            attempt.settle()
            dispatch.close()
            return result
        dispatch.handoff_unresolved()
        return result

    def _persist_cleanup_debt(self, debt: ScratchCleanupDebt) -> None:
        """CAS-persist observed debt before any later cleanup dispatch."""
        updated_call = replace(self._call, scratch_cleanup_debt=debt)
        payload = encode_file_call_obligation(updated_call)
        bound = self._owner.rebind_lifecycle_obligation(
            self._persisted.obligation_id,
            "file-call",
            payload_version=self._persisted.payload_version,
            payload=self._persisted.payload,
        )
        self._pending_cleanup_debt = (updated_call, payload)
        try:
            persisted = bound.publish_payload(
                expected_revision=self._persisted.payload_revision,
                payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                payload=payload,
            )
        except BaseException:
            with suppress(BaseException):
                self._adopt_persisted_cleanup_debt(updated_call, payload)
            raise
        self._adopt_persisted_cleanup_debt(updated_call, payload, persisted=persisted)

    def _reconcile_pending_cleanup_debt(self) -> None:
        """Adopt a publication whose caller lost its reply before dispatching again."""
        pending = self._pending_cleanup_debt
        if pending is None:
            return
        call, payload = pending
        try:
            self._adopt_persisted_cleanup_debt(call, payload)
        except StateError:
            current = self._owner.rebind_lifecycle_obligation(
                self._persisted.obligation_id,
                "file-call",
                payload_version=self._persisted.payload_version,
                payload=self._persisted.payload,
            )
            self._pending_cleanup_debt = None
            persisted = current._persisted_obligation
            if (
                persisted.obligation_id == self._persisted.obligation_id
                and persisted.obligation_kind == self._persisted.obligation_kind
                and persisted.payload_version == self._persisted.payload_version
                and persisted.payload == self._persisted.payload
                and persisted.payload_revision == self._persisted.payload_revision
            ):
                return
            raise

    def _adopt_persisted_cleanup_debt(
        self,
        call: FileCallObligation,
        payload: bytes,
        *,
        persisted: LifecycleObligation | None = None,
    ) -> None:
        """Confirm and adopt only the exact debt payload this adapter intended."""
        if persisted is None:
            rebound = self._owner.rebind_lifecycle_obligation(
                self._persisted.obligation_id,
                "file-call",
                payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                payload=payload,
            )
            persisted = rebound._persisted_obligation
        expected_revision = self._persisted.payload_revision + (payload != self._persisted.payload)
        if (
            persisted.ownership != self._owner.ownership
            or persisted.obligation_id != self._persisted.obligation_id
            or persisted.obligation_kind != "file-call"
            or persisted.state is not LifecycleObligationState.POSSIBLE_EFFECT
            or persisted.payload_version != FILE_CALL_OBLIGATION_PAYLOAD_VERSION
            or persisted.payload != payload
            or persisted.payload_revision != expected_revision
        ):
            raise StateError(
                "download recovery cleanup debt publication is not the intended possible effect",
                entity_kind=self._owner.ownership.scope.resource_kind,
                entity_name=self._owner.ownership.scope.resource_name,
            )
        self._persisted = persisted
        self._call = call
        self._bound = RecoveredLifecycleObligation(self._owner, persisted)
        self._pending_cleanup_debt = None


def _terminated_without_possible_effect(dispatch: Dispatch, completion: ExitStatus | None) -> bool:
    return dispatch is Dispatch.NOT_SENT or (dispatch is Dispatch.SENT and completion == ExitStatus(code=0))
