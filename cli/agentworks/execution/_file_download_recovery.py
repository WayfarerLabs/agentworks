"""Private recovery actions for one retained DOWNLOAD lifecycle effect.

This module deliberately exposes only snapshot reconciliation and exact
cleanup.  It never imports snapshot creation or chunking, and it does not
resolve the durable lifecycle obligation.  The current drain-evidence
constructor is private test substrate, not a production proof source.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.db.operations import LifecycleObligation, OperationOwnership
    from agentworks.execution._managed_runs import ManagedTargetIdentity
    from agentworks.execution._scratch_receipt import ScratchCleanupDebt
    from agentworks.execution.carrier import Carrier, Deadline
    from agentworks.operations import OperationOwner, RecoveredLifecycleObligation


_EVIDENCE_KEY = object()


@dataclass(frozen=True, slots=True)
class _LocalHelperDrainRecord:
    """One test-journal observation for a helper tied to a DOWNLOAD token."""

    helper_id: str
    exited: bool


@dataclass(frozen=True, slots=True, repr=False)
class _DownloadDrainEvidence:
    """Private local-substrate proof for one exact retained DOWNLOAD record.

    Production must supply an adapter-specific proof producer before this
    recovery path is wired into a target.  The private test constructor models
    a journal that independently verified every helper for this token exited.
    """

    _key: object
    ownership: OperationOwnership
    obligation_id: str
    payload_revision: int
    payload: bytes
    target: ManagedTargetIdentity
    token: bytes
    _helper_ids: tuple[str, ...]

    @classmethod
    def _from_local_test(
        cls,
        ownership: OperationOwnership,
        obligation: LifecycleObligation,
        call: FileCallObligation,
        helper_records: tuple[_LocalHelperDrainRecord, ...],
    ) -> _DownloadDrainEvidence:
        """Build test-only evidence after an external journal proved drain."""
        token = call.token
        if call.family is not FileCallFamily.DOWNLOAD or token is None:
            raise ValueError("local drain evidence requires one DOWNLOAD token")
        if not helper_records or any(
            type(record) is not _LocalHelperDrainRecord or not record.helper_id or not record.exited
            for record in helper_records
        ):
            raise ValueError("local drain evidence requires terminated helpers")
        return cls(
            _EVIDENCE_KEY,
            ownership,
            obligation.obligation_id,
            obligation.payload_revision,
            obligation.payload,
            call.target,
            token,
            tuple(record.helper_id for record in helper_records),
        )

    def _require_exact(
        self,
        ownership: OperationOwnership,
        obligation: LifecycleObligation,
        call: FileCallObligation,
        target: ManagedTargetIdentity,
    ) -> None:
        if (
            self._key is not _EVIDENCE_KEY
            or self.ownership != ownership
            or self.obligation_id != obligation.obligation_id
            or self.payload_revision != obligation.payload_revision
            or self.payload != obligation.payload
            or self.target != target
            or call.family is not FileCallFamily.DOWNLOAD
            or call.target != target
            or call.token is None
            or self.token != call.token
        ):
            raise StateError(
                "download recovery drain evidence does not match the retained obligation",
                entity_kind=ownership.scope.resource_kind,
                entity_name=ownership.scope.resource_name,
            )

    def _after_publication(self, obligation: LifecycleObligation) -> _DownloadDrainEvidence:
        """Carry the same drained-helper proof across a local CAS publication."""
        return replace(
            self,
            payload_revision=obligation.payload_revision,
            payload=obligation.payload,
        )


@dataclass(slots=True, repr=False)
class FileDownloadRecovery:
    """One exact DOWNLOAD recovery binding with no generic replay surface."""

    _owner: OperationOwner
    _target: ManagedTargetIdentity
    _persisted: LifecycleObligation
    _call: FileCallObligation
    _evidence: _DownloadDrainEvidence
    _bound: RecoveredLifecycleObligation

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
        return cls(owner, target, obligation, call, evidence, bound)

    def reconcile(self, carrier: Carrier, *, deadline: Deadline) -> FileSnapshotCandidateResult:
        """Observe retained snapshot ownership without snapshot creation."""
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
        debt = None if result.observation is None else result.observation.cleanup_debt
        if debt is not None:
            self._persist_cleanup_debt(debt)
        return result

    def cleanup(self, carrier: Carrier, *, deadline: Deadline) -> FileSnapshotCandidateResult:
        """Attempt exact cleanup only after reconciliation persisted its debt."""
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
        dispatch = self._bound.open_dispatch()
        attempt = dispatch.begin_attempt()
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
        persisted = bound.publish_payload(
            expected_revision=self._persisted.payload_revision,
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=payload,
        )
        self._persisted = persisted
        self._call = updated_call
        self._evidence = self._evidence._after_publication(persisted)
        self._bound = self._owner.rebind_possible_effect_lifecycle_obligation(
            persisted.obligation_id,
            persisted.obligation_kind,
            payload_version=persisted.payload_version,
            payload=persisted.payload,
            payload_revision=persisted.payload_revision,
        )


def _terminated_without_possible_effect(dispatch: Dispatch, completion: ExitStatus | None) -> bool:
    return dispatch is Dispatch.NOT_SENT or (dispatch is Dispatch.SENT and completion == ExitStatus(code=0))
