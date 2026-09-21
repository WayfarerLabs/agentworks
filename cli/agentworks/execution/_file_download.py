"""Private verified file download under borrowed core ownership."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from agentworks.errors import ValidationError
from agentworks.execution._file_paths import normalized_relative_path, normalized_root
from agentworks.execution._file_snapshot_exchange import (
    FileSnapshotObservation,
    FileSnapshotObservationState,
    snapshot_begin,
    snapshot_chunk,
    snapshot_cleanup,
    snapshot_reconcile,
)
from agentworks.execution._file_snapshot_protocol import (
    MAX_PATH_BYTES,
    MAX_SNAPSHOT_CHUNK_BYTES,
    FileSnapshotFailureCode,
    FileSnapshotFailureControl,
)
from agentworks.execution._file_spool import SpoolSnapshotFailureKind
from agentworks.execution._fixed_helper_operation import BorrowedFixedHelperCarrier
from agentworks.execution._helper_launcher import IdentityPlan, _validate_plan
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
)
from agentworks.execution._scratch import ReadyScratchReference, ScratchFailureKind, _cleanup_debt
from agentworks.execution.carrier import Deadline, Dispatch, ExitStatus, Failure
from agentworks.operations import OperationBorrow

if TYPE_CHECKING:
    from agentworks.execution._file_stat import FileRevision
    from agentworks.execution._scratch_receipt import ScratchCleanupDebt
    from agentworks.execution.carrier import ByteSink, Carrier

_MAX_SIZE = (1 << 63) - 1
_RUNTIME_REFUSALS = frozenset(
    {
        RuntimePrerequisiteState.MISSING,
        RuntimePrerequisiteState.SHIM,
        RuntimePrerequisiteState.UNUSABLE,
        RuntimePrerequisiteState.UNSUPPORTED_VERSION,
        RuntimePrerequisiteState.MISSING_MODULES,
    }
)


class _Digest(Protocol):
    def update(self, data: bytes | bytearray | memoryview, /) -> None: ...

    def digest(self) -> bytes: ...


class FileDownloadStatus(StrEnum):
    """Closed private result categories for one download composition."""

    COMPLETE = "complete"
    ABSENT = "absent"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class FileDownloadFailure(StrEnum):
    """Safe primary failure classification without source or sink text."""

    DEADLINE = "deadline"
    SINK = "sink"
    SINK_CONTRACT = "sink_contract"
    SNAPSHOT = "snapshot"
    CLEANUP = "cleanup"
    OBSERVATION = "observation"
    RUNTIME_PREREQUISITE = "runtime_prerequisite"
    TERMINATION = "termination"
    INTEGRITY = "integrity"


class FileDownloadFailurePhase(StrEnum):
    """Snapshot exchange that established the primary download failure."""

    SNAPSHOT_BEGIN = "snapshot_begin"
    SNAPSHOT_CHUNK = "snapshot_chunk"
    SNAPSHOT_RECONCILE = "snapshot_reconcile"
    SNAPSHOT_CLEANUP = "snapshot_cleanup"


@dataclass(frozen=True, slots=True, repr=False)
class FileDownloadBinding:
    """Original core-bound inputs needed to interpret retained facts."""

    trusted_root_path: str
    relative_path: str
    max_bytes: int
    identity_plan: IdentityPlan
    runtime_selection: RuntimeSelection


@dataclass(frozen=True, slots=True, repr=False)
class FileDownloadOutcome:
    """Bounded download facts; downloaded bytes and sink errors are omitted."""

    status: FileDownloadStatus
    binding: FileDownloadBinding
    token: bytes = field(repr=False)
    accepted_bytes: int
    stream_verified: bool = False
    source_revision: FileRevision | None = field(default=None, repr=False)
    ready: ReadyScratchReference | None = field(default=None, repr=False)
    cleanup_debt: ScratchCleanupDebt | None = field(default=None, repr=False)
    snapshot_ownership_uncertain: bool = False
    pending_remote_effects: bool = False
    deadline_exceeded: bool = False
    failure: FileDownloadFailure | None = None
    snapshot_failure: FileSnapshotFailureControl | None = field(default=None, repr=False)
    runtime_prerequisite: RuntimePrerequisiteObservation | None = None
    coordination_uncertain: bool = False
    requires_owner_retention: bool = False
    failure_phase: FileDownloadFailurePhase | None = None
    failure_dispatch: Dispatch | None = None
    carrier_failure: Failure | None = None


class FileDownloadControlFact(Exception):
    """Safe bounded download state attached to escaping control flow."""

    def __init__(self, outcome: FileDownloadOutcome) -> None:
        self.outcome = outcome
        super().__init__("private download stopped with retained operation state")


@dataclass(slots=True, repr=False)
class _WorkingState:
    binding: FileDownloadBinding
    token: bytes
    operation: BorrowedFixedHelperCarrier
    accepted_bytes: int = 0
    stream_verified: bool = False
    absent: bool = False
    source_revision: FileRevision | None = None
    ready: ReadyScratchReference | None = None
    cleanup_debt: ScratchCleanupDebt | None = None
    ownership_uncertain: bool = False
    pending_remote_effects: bool = False
    deadline_exceeded: bool = False
    failure: FileDownloadFailure | None = None
    snapshot_failure: FileSnapshotFailureControl | None = None
    runtime_prerequisite: RuntimePrerequisiteObservation | None = None
    failure_phase: FileDownloadFailurePhase | None = None
    failure_dispatch: Dispatch | None = None
    carrier_failure: Failure | None = None

    def fail(
        self,
        failure: FileDownloadFailure,
        *,
        phase: FileDownloadFailurePhase | None = None,
        dispatch: Dispatch | None = None,
        carrier_failure: Failure | None = None,
    ) -> None:
        if self.failure is None:
            self.failure = failure
            self.failure_phase = phase
            self.failure_dispatch = dispatch
            self.carrier_failure = carrier_failure

    def finish(self) -> FileDownloadOutcome:
        pending = self.pending_remote_effects or self.operation.pending_remote_effects
        coordination_uncertain = self.operation.coordination_uncertain
        retain = (
            pending
            or self.cleanup_debt is not None
            or self.ownership_uncertain
            or self.operation.has_outstanding_attempt
            or coordination_uncertain
        )
        if self.stream_verified and self.failure is None and not retain:
            status = FileDownloadStatus.COMPLETE
        elif self.absent and self.failure is None and not retain:
            status = FileDownloadStatus.ABSENT
        elif pending or self.ownership_uncertain:
            status = FileDownloadStatus.UNCERTAIN
        else:
            status = FileDownloadStatus.FAILED
        return FileDownloadOutcome(
            status=status,
            binding=self.binding,
            token=self.token,
            accepted_bytes=self.accepted_bytes,
            stream_verified=self.stream_verified,
            source_revision=self.source_revision,
            ready=self.ready,
            cleanup_debt=self.cleanup_debt,
            snapshot_ownership_uncertain=self.ownership_uncertain,
            pending_remote_effects=pending,
            deadline_exceeded=self.deadline_exceeded,
            failure=self.failure,
            snapshot_failure=self.snapshot_failure,
            runtime_prerequisite=self.runtime_prerequisite,
            coordination_uncertain=coordination_uncertain,
            requires_owner_retention=retain,
            failure_phase=self.failure_phase,
            failure_dispatch=self.failure_dispatch,
            carrier_failure=self.carrier_failure,
        )


def download_file(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    sink: ByteSink,
    max_bytes: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    borrow: OperationBorrow,
) -> FileDownloadOutcome:
    """Download one bounded immutable snapshot without closing the sink."""
    return _prepare_download(
        carrier,
        trusted_root_path=trusted_root_path,
        relative_path=relative_path,
        sink=sink,
        max_bytes=max_bytes,
        plan=plan,
        deadline=deadline,
        runtime_selection=runtime_selection,
        borrow=borrow,
    ).run()


def _prepare_download(
    carrier: Carrier,
    *,
    trusted_root_path: str,
    relative_path: str,
    sink: ByteSink,
    max_bytes: int,
    plan: IdentityPlan,
    deadline: Deadline,
    runtime_selection: RuntimeSelection,
    borrow: OperationBorrow,
) -> _PreparedDownload:
    """Prepare one validated concrete download without dispatching it."""
    binding = _validate_inputs(
        trusted_root_path,
        relative_path,
        sink,
        max_bytes,
        plan,
        deadline,
        runtime_selection,
        borrow,
    )
    token = secrets.token_bytes(16)
    operation = BorrowedFixedHelperCarrier(carrier, borrow)
    state = _WorkingState(binding, token, operation)
    workflow = _DownloadWorkflow(operation, sink, deadline, state)
    return _PreparedDownload(binding, state, workflow)


@dataclass(slots=True, repr=False)
class _PreparedDownload:
    """Validated download state attachable to core ownership before dispatch."""

    binding: FileDownloadBinding
    state: _WorkingState
    workflow: _DownloadWorkflow

    def run(self) -> FileDownloadOutcome:
        try:
            return self.workflow.run()
        except BaseException as control:
            try:
                self.workflow.note_control_stop()
                if not self.state.operation.coordination_uncertain:
                    try:
                        self.workflow.cleanup_after_local_stop()
                    except BaseException:
                        self.workflow.note_control_stop()
                        self.state.fail(FileDownloadFailure.CLEANUP)
                self.workflow.note_control_stop()
                fact = FileDownloadControlFact(self.state.finish())
            except BaseException:
                # No fact for this call exists. Do not reuse a prior cause as
                # current evidence; core still owns the attached working state.
                raise control from None
            raise control from fact

    def release_sink(self) -> None:
        """Drop the caller sink after core has captured the complete outcome."""
        self.workflow.release_sink()


class _DownloadWorkflow:
    def __init__(
        self,
        carrier: BorrowedFixedHelperCarrier,
        sink: ByteSink,
        deadline: Deadline,
        state: _WorkingState,
    ) -> None:
        self._carrier = carrier
        self._sink: ByteSink | None = sink
        self._deadline = deadline
        self._state = state

    def run(self) -> FileDownloadOutcome:
        if self._expired():
            return self._state.finish()
        if not self._begin():
            self._cleanup_after_failure()
            return self._state.finish()
        if self._state.absent:
            return self._state.finish()
        if not self._transfer():
            self._cleanup_after_failure()
            return self._state.finish()
        self._cleanup_snapshot()
        return self._state.finish()

    def note_control_stop(self) -> None:
        self._state.deadline_exceeded = self._state.deadline_exceeded or self._deadline.expired
        if self._state.operation.outstanding_attempt is not None:
            self._state.pending_remote_effects = True

    def cleanup_after_local_stop(self) -> None:
        if not self._state.pending_remote_effects and not self._state.operation.coordination_uncertain:
            self._cleanup_after_failure()

    def release_sink(self) -> None:
        self._sink = None

    def _begin(self) -> bool:
        result = snapshot_begin(
            self._carrier,
            trusted_root_path=self._state.binding.trusted_root_path,
            relative_path=self._state.binding.relative_path,
            max_bytes=self._state.binding.max_bytes,
            token=self._state.token,
            plan=self._state.binding.identity_plan,
            deadline=self._deadline,
            runtime_selection=self._state.binding.runtime_selection,
        )
        observation = result.observation
        if result.dispatch is not Dispatch.NOT_SENT:
            self._record_runtime(result.runtime_prerequisite)
        if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
            self._record_observation(
                observation,
                phase=FileDownloadFailurePhase.SNAPSHOT_BEGIN,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
        normal = self._settle(result.dispatch, result.carrier_completion)
        if not normal:
            self._state.fail(
                FileDownloadFailure.OBSERVATION
                if result.dispatch is Dispatch.NOT_SENT
                else FileDownloadFailure.TERMINATION,
                phase=FileDownloadFailurePhase.SNAPSHOT_BEGIN,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            return False
        if self._runtime_refused(result.runtime_prerequisite):
            self._state.fail(
                FileDownloadFailure.RUNTIME_PREREQUISITE,
                phase=FileDownloadFailurePhase.SNAPSHOT_BEGIN,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            return False
        if observation is not None and observation.state is FileSnapshotObservationState.READY:
            return True
        if observation is not None and observation.state is FileSnapshotObservationState.ABSENT:
            self._state.absent = True
            return True
        if observation is not None and observation.state is FileSnapshotObservationState.REFUSED:
            self._state.fail(
                FileDownloadFailure.SNAPSHOT,
                phase=FileDownloadFailurePhase.SNAPSHOT_BEGIN,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
        else:
            self._state.fail(
                FileDownloadFailure.OBSERVATION,
                phase=FileDownloadFailurePhase.SNAPSHOT_BEGIN,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
            if result.dispatch is not Dispatch.NOT_SENT:
                self._reconcile_creation()
        return False

    def _transfer(self) -> bool:
        revision = self._state.source_revision
        ready = self._state.ready
        assert revision is not None and revision.digest is not None and ready is not None
        digest = hashlib.sha256()
        offset = 0
        while offset < revision.stat.size:
            length = min(MAX_SNAPSHOT_CHUNK_BYTES, revision.stat.size - offset)
            result = snapshot_chunk(
                self._carrier,
                token=self._state.token,
                ready=ready,
                offset=offset,
                length=length,
                plan=self._state.binding.identity_plan,
                deadline=self._deadline,
                runtime_selection=self._state.binding.runtime_selection,
            )
            observation = result.observation
            if result.dispatch is not Dispatch.NOT_SENT:
                self._record_runtime(result.runtime_prerequisite)
            if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
                self._record_observation(
                    observation,
                    phase=FileDownloadFailurePhase.SNAPSHOT_CHUNK,
                    dispatch=result.dispatch,
                    carrier_failure=result.carrier_failure,
                )
            normal = self._settle(result.dispatch, result.carrier_completion)
            if not normal:
                self._state.fail(
                    FileDownloadFailure.OBSERVATION
                    if result.dispatch is Dispatch.NOT_SENT
                    else FileDownloadFailure.TERMINATION,
                    phase=FileDownloadFailurePhase.SNAPSHOT_CHUNK,
                    dispatch=result.dispatch,
                    carrier_failure=result.carrier_failure,
                )
                return False
            if self._runtime_refused(result.runtime_prerequisite):
                self._state.fail(
                    FileDownloadFailure.RUNTIME_PREREQUISITE,
                    phase=FileDownloadFailurePhase.SNAPSHOT_CHUNK,
                    dispatch=result.dispatch,
                    carrier_failure=result.carrier_failure,
                )
                return False
            if observation is None or observation.state is not FileSnapshotObservationState.CHUNK:
                self._state.fail(
                    FileDownloadFailure.SNAPSHOT
                    if observation is not None and observation.state is FileSnapshotObservationState.REFUSED
                    else FileDownloadFailure.OBSERVATION,
                    phase=FileDownloadFailurePhase.SNAPSHOT_CHUNK,
                    dispatch=result.dispatch,
                    carrier_failure=result.carrier_failure,
                )
                return False
            chunk = observation.chunk
            assert chunk is not None
            if not self._write_chunk(chunk.data, digest):
                return False
            offset += len(chunk.data)
        if offset != revision.stat.size or digest.digest() != revision.digest:
            self._state.fail(FileDownloadFailure.INTEGRITY)
            return False
        self._state.stream_verified = True
        return True

    def _write_chunk(self, data: bytes, digest: _Digest) -> bool:
        sink = self._sink
        assert sink is not None
        offset = 0
        while offset < len(data):
            if self._expired():
                return False
            try:
                written = sink.try_write(memoryview(data)[offset:])
            except Exception:
                self._state.fail(FileDownloadFailure.SINK)
                return False
            if written is None:
                remaining = self._deadline.remaining()
                time.sleep(0.001 if remaining is None else min(0.001, remaining))
                continue
            if type(written) is not int or written <= 0 or written > len(data) - offset:
                self._state.fail(FileDownloadFailure.SINK_CONTRACT)
                return False
            accepted = data[offset : offset + written]
            digest.update(accepted)
            offset += written
            self._state.accepted_bytes += written
        return True

    def _reconcile_creation(self) -> None:
        if self._expired() or self._state.pending_remote_effects:
            self._state.ownership_uncertain = self._state.cleanup_debt is None
            return
        result = snapshot_reconcile(
            self._carrier,
            token=self._state.token,
            plan=self._state.binding.identity_plan,
            deadline=self._deadline,
            runtime_selection=self._state.binding.runtime_selection,
        )
        observation = result.observation
        if result.dispatch is not Dispatch.NOT_SENT:
            self._record_runtime(result.runtime_prerequisite)
        if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
            self._record_observation(
                observation,
                phase=FileDownloadFailurePhase.SNAPSHOT_RECONCILE,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
        normal = self._settle(result.dispatch, result.carrier_completion)
        if normal and self._runtime_refused(result.runtime_prerequisite):
            self._state.fail(
                FileDownloadFailure.RUNTIME_PREREQUISITE,
                phase=FileDownloadFailurePhase.SNAPSHOT_RECONCILE,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
        self._state.ownership_uncertain = self._state.cleanup_debt is None

    def _cleanup_after_failure(self) -> None:
        if (
            self._state.pending_remote_effects
            or self._state.ownership_uncertain
            or self._state.deadline_exceeded
            or self._runtime_refused(self._state.runtime_prerequisite)
        ):
            return
        self._cleanup_snapshot()

    def _cleanup_snapshot(self) -> None:
        debt = self._state.cleanup_debt
        if debt is None or self._state.pending_remote_effects:
            return
        if self._expired():
            return
        result = snapshot_cleanup(
            self._carrier,
            token=self._state.token,
            cleanup_debt=debt,
            plan=self._state.binding.identity_plan,
            deadline=self._deadline,
            runtime_selection=self._state.binding.runtime_selection,
        )
        observation = result.observation
        if result.dispatch is not Dispatch.NOT_SENT:
            self._record_runtime(result.runtime_prerequisite)
        if result.dispatch is not Dispatch.NOT_SENT and observation is not None:
            self._record_observation(
                observation,
                phase=FileDownloadFailurePhase.SNAPSHOT_CLEANUP,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )
        normal = self._settle(result.dispatch, result.carrier_completion)
        if normal and observation is not None and observation.state is FileSnapshotObservationState.CLEANED:
            self._state.cleanup_debt = None
        else:
            self._state.fail(
                FileDownloadFailure.CLEANUP,
                phase=FileDownloadFailurePhase.SNAPSHOT_CLEANUP,
                dispatch=result.dispatch,
                carrier_failure=result.carrier_failure,
            )

    def _record_observation(
        self,
        observation: FileSnapshotObservation,
        *,
        phase: FileDownloadFailurePhase,
        dispatch: Dispatch,
        carrier_failure: Failure | None,
    ) -> None:
        snapshot = observation.snapshot
        if snapshot is not None:
            self._state.ready = snapshot.ready
            self._state.source_revision = snapshot.source
            self._state.cleanup_debt = _cleanup_debt(snapshot.ready)
        if observation.cleanup_debt is not None:
            self._state.cleanup_debt = observation.cleanup_debt
        failure = observation.failure
        if failure is not None:
            if self._state.failure is None and self._state.snapshot_failure is None:
                self._state.snapshot_failure = failure
            if failure.cleanup_debt is not None:
                self._state.cleanup_debt = failure.cleanup_debt
            if self._snapshot_deadline(failure):
                self._state.deadline_exceeded = True
                self._state.fail(
                    FileDownloadFailure.DEADLINE,
                    phase=phase,
                    dispatch=dispatch,
                    carrier_failure=carrier_failure,
                )
        if observation.state is FileSnapshotObservationState.OWNERSHIP_UNCERTAIN:
            self._state.cleanup_debt = None
            self._state.ownership_uncertain = True
        elif observation.state is FileSnapshotObservationState.RECOVERED:
            self._state.ownership_uncertain = False

    def _settle(self, dispatch: Dispatch, completion: ExitStatus | None) -> bool:
        normal = self._state.operation.settle(dispatch, completion)
        self._state.deadline_exceeded = self._state.deadline_exceeded or self._deadline.expired
        self._state.pending_remote_effects = (
            self._state.pending_remote_effects or self._state.operation.pending_remote_effects
        )
        return normal

    def _expired(self) -> bool:
        if not self._deadline.expired:
            return False
        self._state.deadline_exceeded = True
        self._state.fail(FileDownloadFailure.DEADLINE)
        return True

    def _record_runtime(self, observation: RuntimePrerequisiteObservation) -> None:
        current = self._state.runtime_prerequisite
        rank = (
            2 if observation.state in _RUNTIME_REFUSALS else int(observation.state is RuntimePrerequisiteState.UNKNOWN)
        )
        current_rank = (
            -1
            if current is None
            else 2
            if current.state in _RUNTIME_REFUSALS
            else int(current.state is RuntimePrerequisiteState.UNKNOWN)
        )
        if rank > current_rank:
            self._state.runtime_prerequisite = observation

    @staticmethod
    def _runtime_refused(observation: RuntimePrerequisiteObservation | None) -> bool:
        return observation is not None and observation.state in _RUNTIME_REFUSALS

    @staticmethod
    def _snapshot_deadline(failure: FileSnapshotFailureControl) -> bool:
        if failure.code is FileSnapshotFailureCode.SPOOL:
            return failure.spool_kind is SpoolSnapshotFailureKind.DEADLINE
        return failure.code is FileSnapshotFailureCode.SCRATCH and failure.scratch_kind is ScratchFailureKind.DEADLINE


def _validate_inputs(
    trusted_root_path: object,
    relative_path: object,
    sink: object,
    max_bytes: object,
    plan: object,
    deadline: object,
    runtime_selection: object,
    borrow: object,
) -> FileDownloadBinding:
    if type(trusted_root_path) is not str or not normalized_root(trusted_root_path):
        raise ValidationError("Download requires a normalized absolute trusted root")
    if type(relative_path) is not str or not normalized_relative_path(relative_path):
        raise ValidationError("Download requires a normalized nonempty relative path")
    encoding_failed = False
    root_bytes = b""
    relative_bytes = b""
    try:
        root_bytes = trusted_root_path.encode("utf-8")
        relative_bytes = relative_path.encode("utf-8")
    except UnicodeEncodeError:
        encoding_failed = True
    if encoding_failed:
        raise ValidationError("Download paths must be valid UTF-8")
    if len(root_bytes) > MAX_PATH_BYTES or len(relative_bytes) > MAX_PATH_BYTES:
        raise ValidationError("Download path exceeds the file protocol bound")
    if type(max_bytes) is not int or not 0 < max_bytes <= _MAX_SIZE:
        raise ValidationError("Download bound must be a positive bounded integer")
    if type(plan) is not IdentityPlan:
        raise ValidationError("Download requires a bound identity plan")
    _validate_plan(plan)
    if type(deadline) is not Deadline:
        raise ValidationError("Download requires one shared deadline")
    if type(runtime_selection) is not RuntimeSelection:
        raise ValidationError("Download requires a bound runtime selection")
    if type(borrow) is not OperationBorrow:
        raise ValidationError("Download requires an active core operation borrow")
    getter_failed = False
    writer = None
    try:
        writer = getattr(sink, "try_write", None)
    except Exception:
        getter_failed = True
    if getter_failed or not callable(writer):
        raise ValidationError("Download requires a nonblocking byte sink")
    return FileDownloadBinding(trusted_root_path, relative_path, max_bytes, plan, runtime_selection)
