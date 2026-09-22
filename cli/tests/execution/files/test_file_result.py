"""Public file-result reduction at the private outcome boundary."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.errors import (
    AgentworksError,
    ConflictError,
    ErrorDetails,
    ExternalError,
    LimitExceededError,
    PartialMutationError,
    StateError,
    UncertainOutcomeError,
)
from agentworks.execution._account import (
    AccountObservationState,
    FileOwnershipObservation,
    FileOwnershipResolutionResult,
)
from agentworks.execution._account_protocol import FileOwnershipFailure
from agentworks.execution._file_download import (
    FileDownloadBinding,
    FileDownloadFailure,
    FileDownloadFailurePhase,
    FileDownloadOutcome,
    FileDownloadStatus,
)
from agentworks.execution._file_inventory import FileInventoryEntry
from agentworks.execution._file_inventory_exchange import (
    FileInventoryCandidateResult,
    FileInventoryObservation,
    FileInventoryObservationState,
)
from agentworks.execution._file_json import (
    FileJsonBinding,
    FileJsonChange,
    FileJsonFailure,
    FileJsonOutcome,
    FileJsonStatus,
)
from agentworks.execution._file_memory_read import FileMemoryReadOutcome
from agentworks.execution._file_metadata import (
    MetadataEffect,
    MetadataFailureKind,
    MetadataPhase,
    MetadataStep,
)
from agentworks.execution._file_metadata_exchange import (
    FileMetadataCandidateResult,
    FileMetadataObservation,
    FileMetadataObservationState,
)
from agentworks.execution._file_metadata_protocol import (
    FileMetadataFailureCode,
    FileMetadataFailureControl,
    FileMetadataOperation,
)
from agentworks.execution._file_object_exchange import (
    FileObjectCandidateResult,
    FileObjectObservation,
    FileObjectObservationState,
)
from agentworks.execution._file_object_protocol import FileObjectFailureCode, FileObjectFailureControl
from agentworks.execution._file_objects import (
    FileKind as PrivateFileKind,
)
from agentworks.execution._file_objects import (
    FileObjectFailureKind,
    FileObjectPhase,
)
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_operations import (
    FileInventoryBinding,
    FileMetadataBinding,
    FileRemoveBinding,
    FileStatBinding,
    OwnedFileOutcome,
)
from agentworks.execution._file_publication import PublicationFailureKind, PublicationPhase
from agentworks.execution._file_publication_protocol import (
    FilePublicationFailureCode,
    FilePublicationFailureControl,
)
from agentworks.execution._file_read_protocol import FileReadFailure
from agentworks.execution._file_result import (
    reduce_file_inventory,
    reduce_file_metadata,
    reduce_file_remove,
    reduce_file_stat,
)
from agentworks.execution._file_result_transfer import (
    reduce_file_json,
    reduce_file_memory_read,
    reduce_file_upload,
)
from agentworks.execution._file_snapshot_protocol import (
    FileSnapshotFailureCode,
    FileSnapshotFailureControl,
)
from agentworks.execution._file_spool import SpoolSnapshotFailureKind
from agentworks.execution._file_stage_protocol import FileStageFailureCode, FileStageFailureControl
from agentworks.execution._file_stat import FileRevision, FileStat
from agentworks.execution._file_upload import (
    FileUploadBinding,
    FileUploadFailure,
    FileUploadFailurePhase,
    FileUploadOutcome,
    FileUploadStatus,
)
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
)
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    Failure,
    PreparedInvocation,
    Retention,
    SinkOutput,
)
from agentworks.execution.files import Change, FileFailureReason, FileOperationPhase, NewMetadata
from agentworks.operations import OperationOwner
from tests.execution.files._file_read_support import LocalCarrier
from tests.execution.files._runtime_support import runtime_ready_record, runtime_selection
from tests.execution.files._target_support import target_for_owner

_READY = RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, "/usr/bin/python3")
_PLAN = IdentityPlan(IdentityExpectation(1001, 1002, (1002,)), IdentityMode.DIRECT)
_RUNTIME = RuntimeSelection(RuntimeTargetOS.LINUX, "/usr/bin/python3")
_EXIT = ExitStatus(code=0)
_DATA = b"public result"
_STAT = FileStat(1, 2, stat.S_IFREG | 0o640, 1, 1001, 1002, len(_DATA), 3, 4)
_REVISION = FileRevision(_STAT, hashlib.sha256(_DATA).digest())
_METADATA_REVISION = FileRevision(_STAT)
_CONTEXT = {"entity_kind": "workspace file", "entity_name": "settings"}


def _error_details(error: AgentworksError) -> ErrorDetails:
    assert error.details is not None
    return error.details


def test_error_details_reject_free_form_fields_without_changing_plain_errors() -> None:
    assert AgentworksError("safe failure").details is None
    with pytest.raises(TypeError):
        ErrorDetails("observation", FileFailureReason.IO)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ErrorDetails(FileOperationPhase.OBSERVATION, "io")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ErrorDetails(FileOperationPhase.OBSERVATION, FileFailureReason.IO, "changed")  # type: ignore[arg-type]


def _stat_binding() -> FileStatBinding:
    return FileStatBinding("/sensitive/root", "private-name", _PLAN, _RUNTIME)


def _inventory_binding() -> FileInventoryBinding:
    return FileInventoryBinding("/sensitive/root", "private-name", 8, 1, 4096, _PLAN, _RUNTIME)


def _remove_binding() -> FileRemoveBinding:
    return FileRemoveBinding("/sensitive/root", "private-name", PrivateFileKind.REGULAR, _REVISION, _PLAN, _RUNTIME)


def _metadata_binding() -> FileMetadataBinding:
    return FileMetadataBinding(
        FileMetadataOperation.SET_METADATA,
        "/sensitive/root",
        "private-name",
        "secret-owner",
        "secret-group",
        0o640,
        _PLAN,
        _RUNTIME,
    )


def _object_candidate(observation: FileObjectObservation) -> FileObjectCandidateResult:
    return FileObjectCandidateResult(Dispatch.SENT, _EXIT, 0, None, _READY, observation)


def _metadata_candidate(observation: FileMetadataObservation) -> FileMetadataCandidateResult:
    return FileMetadataCandidateResult(Dispatch.SENT, _EXIT, 0, None, _READY, observation)


class _ClosedFailureCarrier:
    def __init__(self, dispatch: Dispatch, failure: Failure, *, admit_runtime: bool = False) -> None:
        self.dispatch = dispatch
        self.failure = failure
        self.admit_runtime = admit_runtime

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del deadline
        if self.admit_runtime:
            assert isinstance(io.output, SinkOutput)
            remaining = memoryview(runtime_ready_record(invocation))
            while remaining:
                written = io.output.stdout.try_write(remaining)
                assert written is not None and written > 0
                remaining = remaining[written:]
        delivered = CapturedOutput(complete=False, retention=Retention.DELIVERED)
        return CarrierReport(
            self.dispatch,
            stdout=delivered,
            stderr=delivered,
            failure=self.failure,
        )


def _upload_outcome(**changes: object) -> FileUploadOutcome:
    binding = FileUploadBinding("/sensitive/root", "private-name", len(_DATA), _PLAN, _RUNTIME)
    outcome = FileUploadOutcome(
        FileUploadStatus.COMPLETE,
        binding,
        b"private-token",
        len(_DATA),
        len(_DATA),
        hashlib.sha256(_DATA).digest(),
        revision=_REVISION,
        publication_confirmed=True,
    )
    return replace(outcome, **changes)


def _json_binding() -> FileJsonBinding:
    return FileJsonBinding("/sensitive/root", "private-name", "replace", True, 4096, 8, _PLAN, _RUNTIME)


def test_complete_stat_inventory_and_removal_project_public_values() -> None:
    observed = FileObjectObservation(
        FileObjectObservationState.PRESENT,
        revision=_METADATA_REVISION,
        object_kind=PrivateFileKind.REGULAR,
    )
    metadata = reduce_file_stat(OwnedFileOutcome(_stat_binding(), _object_candidate(observed)), **_CONTEXT)
    assert metadata is not None
    assert metadata.mode == 0o640

    entry = FileInventoryEntry("child", _METADATA_REVISION)
    inventory = FileInventoryCandidateResult(
        Dispatch.SENT,
        _EXIT,
        0,
        None,
        _READY,
        FileInventoryObservation(FileInventoryObservationState.PRESENT, (entry,)),
    )
    listed = reduce_file_inventory(OwnedFileOutcome(_inventory_binding(), inventory), **_CONTEXT)
    assert tuple(str(item.relative_path) for item in listed) == ("child",)

    removal = FileObjectObservation(FileObjectObservationState.CHANGED)
    assert (
        reduce_file_remove(OwnedFileOutcome(_remove_binding(), _object_candidate(removal)), **_CONTEXT).change
        is Change.CHANGED
    )


@pytest.mark.skipif(sys.platform != "linux", reason="the fixed file helpers require Linux")
def test_real_helpers_reduce_stat_inventory_and_conflict_without_private_state(tmp_path: Path) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    target = root / "target"
    target.write_bytes(_DATA)
    directory = root / "listed"
    directory.mkdir()
    directory.joinpath("child").write_bytes(b"child")
    gid = os.getegid()
    plan = IdentityPlan(
        IdentityExpectation(os.geteuid(), gid, tuple(sorted(set(os.getgroups()) | {gid}))),
        IdentityMode.DIRECT,
    )
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-result-vm"),
        "file-result",
    )
    operation = FileOperation(owner, target_for_owner(owner))
    carrier = LocalCarrier()
    runtime = runtime_selection(sys.executable)
    try:
        present = operation.stat(
            carrier,
            trusted_root_path=str(root),
            relative_path="target",
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime,
        )
        metadata = reduce_file_stat(present, **_CONTEXT)
        assert metadata is not None
        assert metadata.size == len(_DATA)

        inventory = operation.list_directory(
            carrier,
            trusted_root_path=str(root),
            relative_path="listed",
            max_entries=8,
            max_depth=1,
            max_encoded_bytes=4096,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime,
        )
        assert tuple(str(entry.relative_path) for entry in reduce_file_inventory(inventory, **_CONTEXT)) == ("child",)

        assert present.result is not None
        observation = present.result.observation
        assert observation is not None and observation.revision is not None
        target.write_bytes(b"changed")
        refused = operation.remove(
            carrier,
            trusted_root_path=str(root),
            relative_path="target",
            expected_kind=PrivateFileKind.REGULAR,
            expected_revision=observation.revision,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime,
        )
        with pytest.raises(ConflictError):
            reduce_file_remove(refused, **_CONTEXT)
        assert not present.requires_owner_retention
        assert not inventory.requires_owner_retention
        assert not refused.requires_owner_retention
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()
    finally:
        database.close()


def test_real_operation_retains_carrier_failure_across_runtime_unknown_and_mutation_uncertainty(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    owner = OperationOwner.acquire(
        database.operations,
        OperationScope(OperationResourceKind.VM, "file-result-failure-vm"),
        "file-result",
    )
    operation = FileOperation(owner, target_for_owner(owner))
    try:
        stat_outcome = operation.stat(
            _ClosedFailureCarrier(Dispatch.NOT_SENT, Failure.DISPATCH),
            trusted_root_path="/sensitive/root",
            relative_path="private-name",
            plan=_PLAN,
            deadline=Deadline.after(30),
            runtime_selection=_RUNTIME,
        )
        with pytest.raises(ExternalError) as raised_stat:
            reduce_file_stat(stat_outcome, **_CONTEXT)
        assert _error_details(raised_stat.value).reason is FileFailureReason.CARRIER_DISPATCH

        remove_outcome = operation.remove(
            _ClosedFailureCarrier(Dispatch.SENT, Failure.DEADLINE, admit_runtime=True),
            trusted_root_path="/sensitive/root",
            relative_path="private-name",
            expected_kind=PrivateFileKind.REGULAR,
            expected_revision=_REVISION,
            plan=_PLAN,
            deadline=Deadline.after(30),
            runtime_selection=_RUNTIME,
        )
        assert remove_outcome.pending_remote_effects
        with pytest.raises(UncertainOutcomeError) as raised_remove:
            reduce_file_remove(remove_outcome, **_CONTEXT)
        assert raised_remove.value.dispatch is Dispatch.SENT
        assert _error_details(raised_remove.value).reason is FileFailureReason.DEADLINE
    finally:
        database.close()


@pytest.mark.parametrize(
    ("outcome", "exception", "reason"),
    [
        (
            FileDownloadOutcome(
                FileDownloadStatus.ABSENT,
                FileDownloadBinding("/sensitive/root", "private-name", len(_DATA), _PLAN, _RUNTIME),
                b"private-token",
                0,
            ),
            None,
            None,
        ),
        (
            FileDownloadOutcome(
                FileDownloadStatus.FAILED,
                FileDownloadBinding("/sensitive/root", "private-name", len(_DATA), _PLAN, _RUNTIME),
                b"private-token",
                0,
                failure=FileDownloadFailure.SNAPSHOT,
                snapshot_failure=FileSnapshotFailureControl(
                    FileSnapshotFailureCode.SPOOL,
                    spool_kind=SpoolSnapshotFailureKind.LIMIT,
                ),
            ),
            LimitExceededError,
            FileFailureReason.LIMIT,
        ),
        (
            FileDownloadOutcome(
                FileDownloadStatus.FAILED,
                FileDownloadBinding("/sensitive/root", "private-name", len(_DATA), _PLAN, _RUNTIME),
                b"private-token",
                0,
                failure=FileDownloadFailure.SNAPSHOT,
                snapshot_failure=FileSnapshotFailureControl(
                    FileSnapshotFailureCode.SPOOL,
                    spool_kind=SpoolSnapshotFailureKind.UNSUPPORTED_OBJECT,
                ),
            ),
            StateError,
            FileFailureReason.UNSUPPORTED,
        ),
        (
            FileDownloadOutcome(
                FileDownloadStatus.FAILED,
                FileDownloadBinding("/sensitive/root", "private-name", len(_DATA), _PLAN, _RUNTIME),
                b"private-token",
                0,
                failure=FileDownloadFailure.OBSERVATION,
            ),
            ExternalError,
            FileFailureReason.INVALID_RESPONSE,
        ),
    ],
)
def test_read_distinguishes_absence_limits_refusal_and_invalid_observation(
    outcome: FileDownloadOutcome,
    exception: type[Exception] | None,
    reason: FileFailureReason | None,
) -> None:
    wrapped = FileMemoryReadOutcome(outcome, None)
    if exception is None:
        assert reduce_file_memory_read(wrapped, **_CONTEXT) is None
        return
    with pytest.raises(exception) as raised:
        reduce_file_memory_read(wrapped, **_CONTEXT)
    assert raised.value.details.reason is reason  # type: ignore[attr-defined]


def test_inventory_absence_is_not_a_complete_empty_inventory() -> None:
    candidate = FileInventoryCandidateResult(
        Dispatch.SENT,
        _EXIT,
        0,
        None,
        _READY,
        FileInventoryObservation(FileInventoryObservationState.NOT_FOUND),
    )
    with pytest.raises(StateError) as raised:
        reduce_file_inventory(OwnedFileOutcome(_inventory_binding(), candidate), **_CONTEXT)
    assert _error_details(raised.value).reason is FileFailureReason.NOT_FOUND


def test_removal_conflict_and_missing_observation_never_become_unchanged() -> None:
    conflict = FileObjectFailureControl(
        FileObjectFailureCode.OBJECT,
        FileObjectFailureKind.CONFLICT,
        FileObjectPhase.CONDITION,
    )
    refused = FileObjectObservation(FileObjectObservationState.REFUSED, failure=conflict)
    with pytest.raises(ConflictError):
        reduce_file_remove(OwnedFileOutcome(_remove_binding(), _object_candidate(refused)), **_CONTEXT)

    missing = FileObjectCandidateResult(Dispatch.SENT, _EXIT, 0, None, _READY, None)
    with pytest.raises(UncertainOutcomeError) as raised:
        reduce_file_remove(OwnedFileOutcome(_remove_binding(), missing), **_CONTEXT)
    assert raised.value.dispatch is Dispatch.SENT

    removal_failure = FileObjectFailureControl(
        FileObjectFailureCode.OBJECT,
        FileObjectFailureKind.IO,
        FileObjectPhase.REMOVAL,
    )
    refused_removal = FileObjectObservation(FileObjectObservationState.REFUSED, failure=removal_failure)
    with pytest.raises(ExternalError) as raised_removal:
        reduce_file_remove(
            OwnedFileOutcome(_remove_binding(), _object_candidate(refused_removal)),
            **_CONTEXT,
        )
    assert _error_details(raised_removal.value).phase is FileOperationPhase.REMOVAL


def test_metadata_partial_and_uncertain_priorities_preserve_closed_steps() -> None:
    partial_failure = FileMetadataFailureControl(
        FileMetadataFailureCode.METADATA,
        MetadataFailureKind.METADATA,
        MetadataPhase.MODE,
        (MetadataStep.OWNERSHIP,),
    )
    partial = FileMetadataObservation(FileMetadataObservationState.PARTIAL, failure=partial_failure)
    with pytest.raises(PartialMutationError) as raised_partial:
        reduce_file_metadata(OwnedFileOutcome(_metadata_binding(), _metadata_candidate(partial)), **_CONTEXT)
    assert raised_partial.value.completed_steps == (MetadataStep.OWNERSHIP,)

    uncertain_failure = replace(partial_failure, attempted_step=MetadataStep.MODE)
    assert uncertain_failure.effect is MetadataEffect.UNCERTAIN
    uncertain = FileMetadataObservation(FileMetadataObservationState.UNCERTAIN, failure=uncertain_failure)
    with pytest.raises(UncertainOutcomeError) as raised_uncertain:
        reduce_file_metadata(OwnedFileOutcome(_metadata_binding(), _metadata_candidate(uncertain)), **_CONTEXT)
    assert raised_uncertain.value.completed_steps == (MetadataStep.OWNERSHIP,)
    assert raised_uncertain.value.attempted_step is MetadataStep.MODE

    with pytest.raises(UncertainOutcomeError) as raised_coordination:
        reduce_file_metadata(
            OwnedFileOutcome(
                _metadata_binding(),
                _metadata_candidate(partial),
                pending_remote_effects=True,
            ),
            **_CONTEXT,
        )
    assert _error_details(raised_coordination.value).reason is FileFailureReason.REFUSED
    assert raised_coordination.value.completed_steps == (MetadataStep.OWNERSHIP,)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (FileOwnershipFailure.MISSING_OWNER, FileFailureReason.MISSING_OWNER),
        (FileOwnershipFailure.MISSING_GROUP, FileFailureReason.MISSING_GROUP),
    ],
)
def test_ownership_lookup_refusals_are_state_errors_not_mutation_uncertainty(
    failure: FileOwnershipFailure,
    reason: FileFailureReason,
) -> None:
    ownership = FileOwnershipResolutionResult(
        Dispatch.SENT,
        _EXIT,
        0,
        None,
        _READY,
        FileOwnershipObservation(AccountObservationState.REFUSED, failure=failure),
    )
    outcome: OwnedFileOutcome[FileMetadataCandidateResult] = OwnedFileOutcome(
        _metadata_binding(),
        ownership_result=ownership,
        requires_owner_retention=True,
    )
    with pytest.raises(StateError) as raised:
        reduce_file_metadata(outcome, **_CONTEXT)
    assert not isinstance(raised.value, UncertainOutcomeError)
    assert _error_details(raised.value).reason is reason


def test_late_deadline_reports_confirmed_metadata_change_without_returning_success() -> None:
    changed = FileMetadataObservation(FileMetadataObservationState.CHANGED, _METADATA_REVISION)
    outcome = OwnedFileOutcome(_metadata_binding(), _metadata_candidate(changed), deadline_exceeded=True)
    with pytest.raises(ExternalError) as raised:
        reduce_file_metadata(outcome, **_CONTEXT)
    assert _error_details(raised.value).reason is FileFailureReason.DEADLINE
    assert _error_details(raised.value).effect is Change.CHANGED


def test_upload_success_and_confirmed_publication_cleanup_failure_remain_distinct() -> None:
    result = reduce_file_upload(_upload_outcome(), **_CONTEXT)
    assert result.change is Change.CHANGED
    assert result.revision is not None

    failed = _upload_outcome(
        status=FileUploadStatus.FAILED,
        failure=FileUploadFailure.CLEANUP,
        requires_owner_retention=True,
    )
    with pytest.raises(ExternalError) as raised:
        reduce_file_upload(failed, **_CONTEXT)
    assert _error_details(raised.value).effect is Change.CHANGED
    assert _error_details(raised.value).reason is FileFailureReason.CLEANUP


def test_upload_creation_ownership_failures_preserve_closed_lookup_diagnostics() -> None:
    for failure, expected in (
        (FileOwnershipFailure.MISSING_OWNER, FileFailureReason.MISSING_OWNER),
        (FileOwnershipFailure.MISSING_GROUP, FileFailureReason.MISSING_GROUP),
    ):
        ownership = FileOwnershipResolutionResult(
            Dispatch.SENT,
            _EXIT,
            0,
            None,
            _READY,
            FileOwnershipObservation(AccountObservationState.REFUSED, failure=failure),
        )
        outcome = _upload_outcome(
            status=FileUploadStatus.FAILED,
            publication_confirmed=False,
            revision=None,
            failure=FileUploadFailure.OWNERSHIP,
            ownership_result=ownership,
            failure_phase=FileUploadFailurePhase.OWNERSHIP,
            failure_dispatch=Dispatch.SENT,
        )
        with pytest.raises(StateError) as raised:
            reduce_file_upload(outcome, **_CONTEXT)
        assert _error_details(raised.value).phase is FileOperationPhase.OWNERSHIP_LOOKUP
        assert _error_details(raised.value).reason is expected

    runtime = FileOwnershipResolutionResult(
        Dispatch.SENT,
        _EXIT,
        0,
        None,
        RuntimePrerequisiteObservation(RuntimePrerequisiteState.SHIM, None),
        None,
    )
    outcome = _upload_outcome(
        status=FileUploadStatus.FAILED,
        publication_confirmed=False,
        revision=None,
        failure=FileUploadFailure.OWNERSHIP,
        ownership_result=runtime,
        failure_phase=FileUploadFailurePhase.OWNERSHIP,
        failure_dispatch=Dispatch.SENT,
    )
    with pytest.raises(ExternalError) as raised_runtime:
        reduce_file_upload(outcome, **_CONTEXT)
    assert _error_details(raised_runtime.value).reason is FileFailureReason.RUNTIME_SHIM


@pytest.mark.parametrize(
    ("failure_phase", "expected_dispatch"),
    [
        (FileUploadFailurePhase.PUBLICATION, Dispatch.SENT),
        (FileUploadFailurePhase.STAGE_CHUNK, None),
        (FileUploadFailurePhase.STAGE_CLEANUP, None),
    ],
)
def test_upload_uncertainty_exposes_only_publication_dispatch(
    failure_phase: FileUploadFailurePhase,
    expected_dispatch: Dispatch | None,
) -> None:
    outcome = _upload_outcome(
        status=FileUploadStatus.UNCERTAIN,
        publication_confirmed=False,
        publication_uncertain=True,
        revision=None,
        failure=FileUploadFailure.TERMINATION,
        failure_phase=failure_phase,
        failure_dispatch=Dispatch.SENT,
        carrier_failure=Failure.DEADLINE,
        pending_remote_effects=True,
    )
    with pytest.raises(UncertainOutcomeError) as raised:
        reduce_file_upload(outcome, **_CONTEXT)
    assert raised.value.dispatch is expected_dispatch
    assert _error_details(raised.value).reason is FileFailureReason.DEADLINE


def test_upload_late_custody_does_not_mask_confirmed_change_or_first_failure() -> None:
    outcome = _upload_outcome(
        status=FileUploadStatus.FAILED,
        failure=FileUploadFailure.CLEANUP,
        failure_phase=FileUploadFailurePhase.STAGE_CLEANUP,
        failure_dispatch=Dispatch.SENT,
        carrier_failure=Failure.OUTPUT,
        pending_remote_effects=True,
    )
    with pytest.raises(ExternalError) as raised:
        reduce_file_upload(outcome, **_CONTEXT)
    details = _error_details(raised.value)
    assert details.phase is FileOperationPhase.CLEANUP
    assert details.reason is FileFailureReason.CARRIER_OUTPUT
    assert details.effect is Change.CHANGED


def test_staging_coordination_uncertainty_is_not_destination_uncertainty() -> None:
    failed = _upload_outcome(
        status=FileUploadStatus.UNCERTAIN,
        publication_confirmed=False,
        revision=None,
        failure=FileUploadFailure.STAGE,
        pending_remote_effects=True,
    )
    with pytest.raises(StateError) as raised:
        reduce_file_upload(failed, **_CONTEXT)
    assert not isinstance(raised.value, UncertainOutcomeError)
    assert _error_details(raised.value).reason is FileFailureReason.REFUSED


def test_memory_read_and_json_preserve_data_revision_and_changed_state() -> None:
    download = FileDownloadOutcome(
        FileDownloadStatus.COMPLETE,
        FileDownloadBinding("/sensitive/root", "private-name", len(_DATA), _PLAN, _RUNTIME),
        b"private-token",
        len(_DATA),
        True,
        _REVISION,
    )
    read = reduce_file_memory_read(FileMemoryReadOutcome(download, _DATA), **_CONTEXT)
    assert read is not None
    assert read.data == _DATA

    json_outcome = FileJsonOutcome(
        FileJsonStatus.COMPLETE,
        FileJsonBinding("/sensitive/root", "private-name", "replace", True, 4096, 8, _PLAN, _RUNTIME),
        FileJsonChange.CHANGED,
        _REVISION,
    )
    result = reduce_file_json(json_outcome, **_CONTEXT)
    assert result.change is Change.CHANGED
    assert result.revision is not None


def test_json_uses_only_its_terminal_upload_for_public_failure_provenance() -> None:
    ownership = FileOwnershipResolutionResult(
        Dispatch.SENT,
        _EXIT,
        0,
        None,
        _READY,
        FileOwnershipObservation(
            AccountObservationState.REFUSED,
            failure=FileOwnershipFailure.MISSING_OWNER,
        ),
    )
    nested_ownership = _upload_outcome(
        status=FileUploadStatus.FAILED,
        publication_confirmed=False,
        revision=None,
        failure=FileUploadFailure.OWNERSHIP,
        ownership_result=ownership,
        failure_phase=FileUploadFailurePhase.OWNERSHIP,
        failure_dispatch=Dispatch.SENT,
    )
    with pytest.raises(StateError) as raised_ownership:
        reduce_file_json(
            FileJsonOutcome(
                FileJsonStatus.FAILED,
                _json_binding(),
                failure=FileJsonFailure.UPLOAD,
                upload_outcome=nested_ownership,
            ),
            **_CONTEXT,
        )
    assert _error_details(raised_ownership.value).phase is FileOperationPhase.OWNERSHIP_LOOKUP
    assert _error_details(raised_ownership.value).reason is FileFailureReason.MISSING_OWNER

    nested_stage = _upload_outcome(
        status=FileUploadStatus.FAILED,
        publication_confirmed=False,
        revision=None,
        failure=FileUploadFailure.STAGE,
        failure_phase=FileUploadFailurePhase.STAGE_BEGIN,
        stage_failure=FileStageFailureControl(FileStageFailureCode.ROOT_REFUSED),
    )
    with pytest.raises(StateError) as raised_stage:
        reduce_file_json(
            FileJsonOutcome(
                FileJsonStatus.FAILED,
                _json_binding(),
                failure=FileJsonFailure.UPLOAD,
                upload_outcome=nested_stage,
            ),
            **_CONTEXT,
        )
    assert _error_details(raised_stage.value).phase is FileOperationPhase.TRANSFER
    assert _error_details(raised_stage.value).reason is FileFailureReason.REFUSED

    nested_carrier = _upload_outcome(
        status=FileUploadStatus.FAILED,
        publication_confirmed=False,
        revision=None,
        failure=FileUploadFailure.TERMINATION,
        failure_phase=FileUploadFailurePhase.STAGE_CHUNK,
        carrier_failure=Failure.DEADLINE,
    )
    with pytest.raises(ExternalError) as raised_carrier:
        reduce_file_json(
            FileJsonOutcome(
                FileJsonStatus.FAILED,
                _json_binding(),
                failure=FileJsonFailure.UPLOAD,
                upload_outcome=nested_carrier,
            ),
            **_CONTEXT,
        )
    assert _error_details(raised_carrier.value).phase is FileOperationPhase.TRANSFER
    assert _error_details(raised_carrier.value).reason is FileFailureReason.DEADLINE

    stale_conflict = _upload_outcome(
        status=FileUploadStatus.FAILED,
        publication_confirmed=False,
        revision=None,
        failure=FileUploadFailure.PUBLICATION,
        failure_phase=FileUploadFailurePhase.PUBLICATION,
        publication_failure=FilePublicationFailureControl(
            FilePublicationFailureCode.PUBLICATION,
            publication_kind=PublicationFailureKind.CONFLICT,
            publication_phase=PublicationPhase.CONDITION,
        ),
    )
    with pytest.raises(LimitExceededError) as raised_read:
        reduce_file_json(
            FileJsonOutcome(
                FileJsonStatus.FAILED,
                _json_binding(),
                failure=FileJsonFailure.READ,
                read_failure=FileReadFailure.LIMIT,
                upload_outcome=stale_conflict,
            ),
            **_CONTEXT,
        )
    assert _error_details(raised_read.value).phase is FileOperationPhase.OBSERVATION
    assert _error_details(raised_read.value).reason is FileFailureReason.LIMIT


def test_complete_download_late_deadline_and_cleanup_debt_never_publish_success() -> None:
    complete = FileDownloadOutcome(
        FileDownloadStatus.COMPLETE,
        FileDownloadBinding("/sensitive/root", "private-name", len(_DATA), _PLAN, _RUNTIME),
        b"private-token",
        len(_DATA),
        True,
        _REVISION,
    )
    with pytest.raises(ExternalError) as raised_deadline:
        reduce_file_memory_read(
            FileMemoryReadOutcome(replace(complete, deadline_exceeded=True), _DATA),
            **_CONTEXT,
        )
    assert _error_details(raised_deadline.value).reason is FileFailureReason.DEADLINE

    cleanup_failed = replace(
        complete,
        status=FileDownloadStatus.FAILED,
        failure=FileDownloadFailure.CLEANUP,
        requires_owner_retention=True,
    )
    with pytest.raises(ExternalError) as raised_cleanup:
        reduce_file_memory_read(FileMemoryReadOutcome(cleanup_failed, _DATA), **_CONTEXT)
    assert _error_details(raised_cleanup.value).reason is FileFailureReason.CLEANUP


@pytest.mark.parametrize(
    ("changes", "phase", "reason", "exception"),
    [
        (
            {
                "failure": FileDownloadFailure.TERMINATION,
                "failure_phase": FileDownloadFailurePhase.SNAPSHOT_CHUNK,
                "failure_dispatch": Dispatch.SENT,
                "carrier_failure": Failure.OUTPUT_LIMIT,
            },
            FileOperationPhase.TRANSFER,
            FileFailureReason.CARRIER_OUTPUT_LIMIT,
            ExternalError,
        ),
        (
            {
                "failure": FileDownloadFailure.OBSERVATION,
                "failure_phase": FileDownloadFailurePhase.SNAPSHOT_BEGIN,
                "failure_dispatch": Dispatch.NOT_SENT,
            },
            FileOperationPhase.OBSERVATION,
            FileFailureReason.CARRIER_DISPATCH,
            ExternalError,
        ),
        (
            {
                "failure": FileDownloadFailure.SNAPSHOT,
                "snapshot_failure": FileSnapshotFailureControl(
                    FileSnapshotFailureCode.SPOOL,
                    spool_kind=SpoolSnapshotFailureKind.LIMIT,
                ),
                "failure_phase": FileDownloadFailurePhase.SNAPSHOT_CHUNK,
                "failure_dispatch": Dispatch.SENT,
                "pending_remote_effects": True,
                "requires_owner_retention": True,
            },
            FileOperationPhase.TRANSFER,
            FileFailureReason.LIMIT,
            LimitExceededError,
        ),
    ],
)
def test_download_preserves_first_exchange_failure_ahead_of_later_custody_state(
    changes: dict[str, object],
    phase: FileOperationPhase,
    reason: FileFailureReason,
    exception: type[AgentworksError],
) -> None:
    outcome = FileDownloadOutcome(
        FileDownloadStatus.FAILED,
        FileDownloadBinding("/sensitive/root", "private-name", len(_DATA), _PLAN, _RUNTIME),
        b"private-token",
        0,
    )
    with pytest.raises(exception) as raised:
        reduce_file_memory_read(FileMemoryReadOutcome(replace(outcome, **changes)), **_CONTEXT)
    assert _error_details(raised.value).phase is phase
    assert _error_details(raised.value).reason is reason
    assert not isinstance(raised.value, UncertainOutcomeError)


def test_runtime_failures_keep_closed_reasons_without_connectivity_claims() -> None:
    for runtime, expected in (
        (RuntimePrerequisiteState.MISSING, FileFailureReason.RUNTIME_MISSING),
        (RuntimePrerequisiteState.SHIM, FileFailureReason.RUNTIME_SHIM),
        (RuntimePrerequisiteState.UNSUPPORTED_VERSION, FileFailureReason.RUNTIME_UNSUPPORTED_VERSION),
    ):
        outcome = FileDownloadOutcome(
            FileDownloadStatus.FAILED,
            FileDownloadBinding("/sensitive/root", "private-name", len(_DATA), _PLAN, _RUNTIME),
            b"private-token",
            0,
            failure=FileDownloadFailure.RUNTIME_PREREQUISITE,
            runtime_prerequisite=RuntimePrerequisiteObservation(runtime, None),
        )
        with pytest.raises(ExternalError) as raised:
            reduce_file_memory_read(FileMemoryReadOutcome(outcome), **_CONTEXT)
        assert _error_details(raised.value).reason is expected
        assert raised.value.hint is not None


def test_public_errors_and_metadata_repr_do_not_retain_sensitive_private_state() -> None:
    metadata = NewMetadata("secret-owner", "secret-group", 0o640)
    assert "secret-owner" not in repr(metadata)
    assert "secret-group" not in repr(metadata)

    outcome = FileDownloadOutcome(
        FileDownloadStatus.FAILED,
        FileDownloadBinding("/sensitive/root", "private-name", len(_DATA), _PLAN, _RUNTIME),
        b"private-token",
        0,
        failure=FileDownloadFailure.OBSERVATION,
    )
    with pytest.raises(ExternalError) as raised:
        reduce_file_memory_read(FileMemoryReadOutcome(outcome), **_CONTEXT)
    error = raised.value
    representation = repr(error)
    attributes = repr(vars(error))
    for secret in ("/sensitive/root", "private-name", "secret-owner", "secret-group", _DATA.decode(), repr(outcome)):
        assert secret not in representation
        assert secret not in attributes
    assert not hasattr(error, "outcome")
