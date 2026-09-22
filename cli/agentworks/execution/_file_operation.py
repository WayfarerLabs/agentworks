"""Core-owned state for concrete private file calls."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from agentworks.errors import ValidationError
from agentworks.execution._file_download import (
    FileDownloadBinding,
    FileDownloadControlFact,
    FileDownloadOutcome,
    _prepare_download,
    _PreparedDownload,
)
from agentworks.execution._file_json import (
    FileJsonBinding,
    FileJsonControlFact,
    FileJsonOutcome,
    JsonFileStrategy,
    _prepare_json_update,
    _PreparedJsonUpdate,
)
from agentworks.execution._file_metadata_protocol import FileMetadataOperation
from agentworks.execution._file_obligation import (
    FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    FileCallFamily,
    FileCallObligation,
    FileCallUncertainty,
    encode_file_call_obligation,
)
from agentworks.execution._file_operations import (
    FileInventoryBinding,
    FileMetadataBinding,
    FileRemoveBinding,
    FileStatBinding,
    OwnedFileBinding,
    OwnedFileControlFact,
    OwnedFileOutcome,
    _prepare_inventory,
    _prepare_metadata,
    _prepare_remove,
    _prepare_stat,
    _PreparedInventory,
    _PreparedMetadata,
    _PreparedRemove,
    _PreparedStat,
)
from agentworks.execution._file_upload import (
    FileUploadBinding,
    FileUploadControlFact,
    FileUploadOutcome,
    _prepare_upload,
    _PreparedUpload,
)
from agentworks.execution._managed_runs import ManagedTargetIdentity
from agentworks.operations import LifecycleObligation, release_borrow_after_custody

if TYPE_CHECKING:
    from agentworks.execution._file_inventory_exchange import FileInventoryCandidateResult
    from agentworks.execution._file_metadata_exchange import FileMetadataCandidateResult
    from agentworks.execution._file_object_exchange import FileObjectCandidateResult
    from agentworks.execution._file_objects import FileKind
    from agentworks.execution._file_publication import Create, Match, Replace
    from agentworks.execution._file_publication_wire import BoundPublicationCleanupDebt
    from agentworks.execution._file_stat import FileRevision
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution._scratch import ScratchReference
    from agentworks.execution._scratch_receipt import ScratchCleanupDebt
    from agentworks.execution.carrier import ByteSink, ByteSource, Carrier, Deadline
    from agentworks.execution.files import NewMetadata
    from agentworks.operations import OperationBorrow, OperationOwner


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedFileDownload:
    """Captured custody for one download with unfinished responsibility."""

    carrier: Carrier = field(repr=False)
    binding: FileDownloadBinding
    outcome: FileDownloadOutcome = field(repr=False)


@dataclass(slots=True, repr=False)
class _ActiveFileCall[BindingT, PreparedT, OutcomeT]:
    carrier: Carrier
    binding: BindingT
    borrow: OperationBorrow
    prepared: PreparedT
    obligation_id: str | None = None
    obligation: LifecycleObligation | None = None
    outcome: OutcomeT | None = None


class _FileCallBinding(Protocol):
    @property
    def trusted_root_path(self) -> str: ...

    @property
    def relative_path(self) -> str: ...

    @property
    def identity_plan(self) -> IdentityPlan: ...

    @property
    def runtime_selection(self) -> RuntimeSelection: ...


type _ActiveFileDownload = _ActiveFileCall[FileDownloadBinding, _PreparedDownload, FileDownloadOutcome]


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedFileUpload:
    """Captured custody for one upload with unfinished responsibility."""

    carrier: Carrier = field(repr=False)
    binding: FileUploadBinding
    outcome: FileUploadOutcome = field(repr=False)


type _ActiveFileUpload = _ActiveFileCall[FileUploadBinding, _PreparedUpload, FileUploadOutcome]


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedFileJsonUpdate:
    """Captured custody for one JSON update with unfinished responsibility."""

    carrier: Carrier = field(repr=False)
    binding: FileJsonBinding
    outcome: FileJsonOutcome = field(repr=False)


type _ActiveFileJsonUpdate = _ActiveFileCall[FileJsonBinding, _PreparedJsonUpdate, FileJsonOutcome]

type _ActiveFileStat = _ActiveFileCall[FileStatBinding, _PreparedStat, OwnedFileOutcome[FileObjectCandidateResult]]
type _ActiveFileInventory = _ActiveFileCall[
    FileInventoryBinding, _PreparedInventory, OwnedFileOutcome[FileInventoryCandidateResult]
]
type _ActiveFileRemove = _ActiveFileCall[
    FileRemoveBinding, _PreparedRemove, OwnedFileOutcome[FileObjectCandidateResult]
]
type _ActiveFileMetadata = _ActiveFileCall[
    FileMetadataBinding, _PreparedMetadata, OwnedFileOutcome[FileMetadataCandidateResult]
]
type _OwnedFileOutcome = (
    OwnedFileOutcome[FileObjectCandidateResult]
    | OwnedFileOutcome[FileInventoryCandidateResult]
    | OwnedFileOutcome[FileMetadataCandidateResult]
)


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedOwnedFile:
    """Captured custody for one single-file call with unfinished responsibility."""

    carrier: Carrier = field(repr=False)
    binding: OwnedFileBinding
    outcome: _OwnedFileOutcome = field(repr=False)


class FileOperation:
    """One core file state shared by all views of an existing owner."""

    def __init__(self, owner: OperationOwner, target: ManagedTargetIdentity) -> None:
        """Bind file calls to one exact owner scope and managed target."""
        scope = owner.ownership.scope
        if (
            type(target) is not ManagedTargetIdentity
            or target.kind.value != scope.resource_kind.value
            or target.name != scope.resource_name
        ):
            raise ValidationError("File operation target must match its owner scope")
        self._owner = owner
        self._target = target
        self._active_downloads: dict[int, _ActiveFileDownload] = {}
        self._unfinished_downloads: list[UnfinishedFileDownload] = []
        self._active_uploads: dict[int, _ActiveFileUpload] = {}
        self._unfinished_uploads: list[UnfinishedFileUpload] = []
        self._active_json_updates: dict[int, _ActiveFileJsonUpdate] = {}
        self._unfinished_json_updates: list[UnfinishedFileJsonUpdate] = []
        self._active_stats: dict[int, _ActiveFileStat] = {}
        self._active_inventories: dict[int, _ActiveFileInventory] = {}
        self._active_removals: dict[int, _ActiveFileRemove] = {}
        self._active_metadata: dict[int, _ActiveFileMetadata] = {}
        self._unfinished_owned_files: list[UnfinishedOwnedFile] = []

    @property
    def active_downloads(self) -> tuple[_ActiveFileDownload, ...]:
        return tuple(self._active_downloads.values())

    @property
    def unfinished_downloads(self) -> tuple[UnfinishedFileDownload, ...]:
        return tuple(self._unfinished_downloads)

    @property
    def active_uploads(self) -> tuple[_ActiveFileUpload, ...]:
        return tuple(self._active_uploads.values())

    @property
    def unfinished_uploads(self) -> tuple[UnfinishedFileUpload, ...]:
        return tuple(self._unfinished_uploads)

    @property
    def active_json_updates(self) -> tuple[_ActiveFileJsonUpdate, ...]:
        return tuple(self._active_json_updates.values())

    @property
    def unfinished_json_updates(self) -> tuple[UnfinishedFileJsonUpdate, ...]:
        return tuple(self._unfinished_json_updates)

    @property
    def active_stats(self) -> tuple[_ActiveFileStat, ...]:
        return tuple(self._active_stats.values())

    @property
    def active_inventories(self) -> tuple[_ActiveFileInventory, ...]:
        return tuple(self._active_inventories.values())

    @property
    def active_removals(self) -> tuple[_ActiveFileRemove, ...]:
        return tuple(self._active_removals.values())

    @property
    def active_metadata(self) -> tuple[_ActiveFileMetadata, ...]:
        return tuple(self._active_metadata.values())

    @property
    def unfinished_owned_files(self) -> tuple[UnfinishedOwnedFile, ...]:
        return tuple(self._unfinished_owned_files)

    def download(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        sink: ByteSink,
        max_bytes: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> FileDownloadOutcome:
        """Run and capture one concrete download under a whole-call borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_download(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                sink=sink,
                max_bytes=max_bytes,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
            )
            active: _ActiveFileDownload = _ActiveFileCall(carrier, prepared.binding, borrow, prepared)
        except BaseException:
            borrow.close()
            raise

        try:
            self._active_downloads[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, FileCallFamily.DOWNLOAD, token=prepared.state.token)

        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, FileDownloadControlFact):
                try:
                    self._capture(active, fact.outcome)
                except BaseException:
                    raise control from fact
            raise

        self._capture(active, outcome)
        return outcome

    def upload(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        source: ByteSource,
        size: int,
        condition: Create | Replace | Match,
        create_metadata: NewMetadata,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> FileUploadOutcome:
        """Run and capture one concrete upload under a whole-call borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_upload(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                source=source,
                size=size,
                condition=condition,
                create_metadata=create_metadata,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
            )
            active: _ActiveFileUpload = _ActiveFileCall(carrier, prepared.binding, borrow, prepared)
        except BaseException:
            borrow.close()
            raise

        try:
            self._active_uploads[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, FileCallFamily.UPLOAD, token=prepared.state.token)

        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, FileUploadControlFact):
                try:
                    self._capture_upload(active, fact.outcome)
                except BaseException:
                    raise control from fact
            raise

        self._capture_upload(active, outcome)
        return outcome

    def update_json(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        source: bytes,
        strategy: JsonFileStrategy,
        create: bool,
        create_metadata: NewMetadata,
        max_bytes: int,
        max_depth: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> FileJsonOutcome:
        """Run and capture one JSON update under a whole-call borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_json_update(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                source=source,
                strategy=strategy,
                create=create,
                create_metadata=create_metadata,
                max_bytes=max_bytes,
                max_depth=max_depth,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
            )
            active: _ActiveFileJsonUpdate = _ActiveFileCall(carrier, prepared.binding, borrow, prepared)
        except BaseException:
            borrow.close()
            raise

        try:
            self._active_json_updates[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, FileCallFamily.JSON_UPDATE)
        prepared.set_child_upload_callback(lambda child, attempt: self._publish_json_child(active, child, attempt))

        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, FileJsonControlFact):
                try:
                    self._capture_json_update(active, fact.outcome)
                except BaseException:
                    raise control from fact
            raise

        self._capture_json_update(active, outcome)
        return outcome

    def stat(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> OwnedFileOutcome[FileObjectCandidateResult]:
        """Run and capture one object observation under a whole-call borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_stat(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
            )
            active: _ActiveFileStat = _ActiveFileCall(carrier, prepared.binding, borrow, prepared)
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_stats[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, FileCallFamily.STAT)
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    self._capture_owned(active, fact.outcome, FileCallFamily.STAT)
                    self._active_stats.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        self._capture_owned(active, outcome, FileCallFamily.STAT)
        self._active_stats.pop(id(active))
        return outcome

    def list_directory(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        max_entries: int,
        max_depth: int,
        max_encoded_bytes: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> OwnedFileOutcome[FileInventoryCandidateResult]:
        """Run and capture one directory inventory under a whole-call borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_inventory(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                max_entries=max_entries,
                max_depth=max_depth,
                max_encoded_bytes=max_encoded_bytes,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
            )
            active: _ActiveFileInventory = _ActiveFileCall(carrier, prepared.binding, borrow, prepared)
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_inventories[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, FileCallFamily.INVENTORY)
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    self._capture_owned(active, fact.outcome, FileCallFamily.INVENTORY)
                    self._active_inventories.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        self._capture_owned(active, outcome, FileCallFamily.INVENTORY)
        self._active_inventories.pop(id(active))
        return outcome

    def remove(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        expected_kind: FileKind,
        expected_revision: FileRevision,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> OwnedFileOutcome[FileObjectCandidateResult]:
        """Run and capture one conditional removal under a whole-call borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_remove(
                carrier,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                expected_kind=expected_kind,
                expected_revision=expected_revision,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
            )
            active: _ActiveFileRemove = _ActiveFileCall(carrier, prepared.binding, borrow, prepared)
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_removals[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, FileCallFamily.REMOVE)
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    self._capture_owned(active, fact.outcome, FileCallFamily.REMOVE)
                    self._active_removals.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        self._capture_owned(active, outcome, FileCallFamily.REMOVE)
        self._active_removals.pop(id(active))
        return outcome

    def set_metadata(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        trusted_owner: str,
        trusted_group: str,
        mode: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> OwnedFileOutcome[FileMetadataCandidateResult]:
        """Resolve and capture one metadata convergence under one borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_metadata(
                carrier,
                operation=FileMetadataOperation.SET_METADATA,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                trusted_owner=trusted_owner,
                trusted_group=trusted_group,
                mode=mode,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
            )
            active: _ActiveFileMetadata = _ActiveFileCall(carrier, prepared.binding, borrow, prepared)
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_metadata[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, FileCallFamily.SET_METADATA)
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    self._capture_owned(active, fact.outcome, FileCallFamily.SET_METADATA)
                    self._active_metadata.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        self._capture_owned(active, outcome, FileCallFamily.SET_METADATA)
        self._active_metadata.pop(id(active))
        return outcome

    def ensure_directory(
        self,
        carrier: Carrier,
        *,
        trusted_root_path: str,
        relative_path: str,
        trusted_owner: str,
        trusted_group: str,
        mode: int,
        plan: IdentityPlan,
        deadline: Deadline,
        runtime_selection: RuntimeSelection,
    ) -> OwnedFileOutcome[FileMetadataCandidateResult]:
        """Resolve and capture one directory convergence under one borrow."""
        borrow = self._owner.borrow()
        try:
            prepared = _prepare_metadata(
                carrier,
                operation=FileMetadataOperation.ENSURE_DIRECTORY,
                trusted_root_path=trusted_root_path,
                relative_path=relative_path,
                trusted_owner=trusted_owner,
                trusted_group=trusted_group,
                mode=mode,
                plan=plan,
                deadline=deadline,
                runtime_selection=runtime_selection,
                borrow=borrow,
            )
            active: _ActiveFileMetadata = _ActiveFileCall(carrier, prepared.binding, borrow, prepared)
        except BaseException:
            borrow.close()
            raise
        try:
            self._active_metadata[id(active)] = active
        except BaseException:
            borrow.close()
            raise
        self._install(active, FileCallFamily.ENSURE_DIRECTORY)
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    self._capture_owned(active, fact.outcome, FileCallFamily.ENSURE_DIRECTORY)
                    self._active_metadata.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        self._capture_owned(active, outcome, FileCallFamily.ENSURE_DIRECTORY)
        self._active_metadata.pop(id(active))
        return outcome

    def _install[BindingT: _FileCallBinding, PreparedT, OutcomeT](
        self,
        active: _ActiveFileCall[BindingT, PreparedT, OutcomeT],
        family: FileCallFamily,
        *,
        token: bytes | None = None,
        attempt: int | None = None,
    ) -> None:
        obligation = self._obligation(family, active.binding, token=token, attempt=attempt)
        if active.obligation_id is None:
            active.obligation_id = secrets.token_hex(16)
        active.obligation = active.borrow.install_dispatch_obligation(
            active.obligation_id,
            "file-call",
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=encode_file_call_obligation(obligation),
        )

    def _publish_json_child(
        self,
        active: _ActiveFileJsonUpdate,
        child: _PreparedUpload,
        attempt: int,
    ) -> None:
        obligation = self._require_obligation(active)
        payload = encode_file_call_obligation(
            self._obligation(
                FileCallFamily.JSON_UPDATE,
                active.binding,
                token=child.state.token,
                attempt=attempt,
            )
        )
        obligation.publish_payload(
            expected_revision=obligation.payload_revision,
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=payload,
        )

    def _obligation(
        self,
        family: FileCallFamily,
        binding: _FileCallBinding,
        *,
        token: bytes | None = None,
        attempt: int | None = None,
        uncertainty: frozenset[FileCallUncertainty] = frozenset(),
        scratch_reference: ScratchReference | None = None,
        scratch_cleanup_debt: ScratchCleanupDebt | None = None,
        publication_cleanup_debt: BoundPublicationCleanupDebt | None = None,
    ) -> FileCallObligation:
        return FileCallObligation(
            family=family,
            target=self._target,
            root=binding.trusted_root_path,
            relative_path=binding.relative_path,
            identity_plan=binding.identity_plan,
            runtime_selection=binding.runtime_selection,
            token=token,
            attempt=attempt,
            scratch_reference=scratch_reference,
            scratch_cleanup_debt=scratch_cleanup_debt,
            publication_cleanup_debt=publication_cleanup_debt,
            uncertainty=uncertainty,
        )

    @staticmethod
    def _require_obligation[BindingT, PreparedT, OutcomeT](
        active: _ActiveFileCall[BindingT, PreparedT, OutcomeT],
    ) -> LifecycleObligation:
        obligation = active.obligation
        if obligation is None:
            raise AssertionError("attached file call has no lifecycle obligation")
        return obligation

    def _publish_retained[BindingT, PreparedT, OutcomeT](
        self,
        active: _ActiveFileCall[BindingT, PreparedT, OutcomeT],
        recovery: FileCallObligation,
    ) -> None:
        obligation = self._require_obligation(active)
        obligation.publish_payload(
            expected_revision=obligation.payload_revision,
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=encode_file_call_obligation(recovery),
        )

    def _release_custody[BindingT, PreparedT, OutcomeT](
        self,
        active: _ActiveFileCall[BindingT, PreparedT, OutcomeT],
        *,
        retain_effect: bool,
    ) -> None:
        if retain_effect and not active.borrow.has_outstanding_attempt:
            retained_attempt = active.borrow.begin_attempt()
            retained_attempt.settle()
        release_borrow_after_custody(active.borrow, retain_effect=retain_effect)

    @staticmethod
    def _uncertainty(
        *,
        pending_remote_effects: bool,
        coordination_uncertain: bool,
    ) -> frozenset[FileCallUncertainty]:
        result: set[FileCallUncertainty] = set()
        if pending_remote_effects:
            result.add(FileCallUncertainty.PENDING_REMOTE_EFFECT)
        if coordination_uncertain:
            result.add(FileCallUncertainty.COORDINATION_UNCERTAINTY)
        return frozenset(result)

    def _upload_uncertainty(self, outcome: FileUploadOutcome) -> frozenset[FileCallUncertainty]:
        result = set(
            self._uncertainty(
                pending_remote_effects=outcome.pending_remote_effects,
                coordination_uncertain=outcome.coordination_uncertain,
            )
        )
        if outcome.stage_ownership_uncertain or outcome.scratch_cleanup_debt is not None:
            result.add(FileCallUncertainty.SCRATCH_OWNERSHIP)
        if outcome.publication_ownership_uncertain or outcome.publication_cleanup_debt is not None:
            result.add(FileCallUncertainty.PUBLICATION_OWNERSHIP)
        return frozenset(result)

    def _capture_owned[BindingT: _FileCallBinding, PreparedT, ResultT](
        self,
        active: _ActiveFileCall[BindingT, PreparedT, OwnedFileOutcome[ResultT]],
        outcome: OwnedFileOutcome[ResultT],
        family: FileCallFamily,
    ) -> None:
        active.outcome = outcome
        if outcome.requires_owner_retention:
            self._publish_retained(
                active,
                self._obligation(
                    family,
                    active.binding,
                    uncertainty=self._uncertainty(
                        pending_remote_effects=outcome.pending_remote_effects,
                        coordination_uncertain=outcome.coordination_uncertain,
                    ),
                ),
            )
            self._unfinished_owned_files.append(
                UnfinishedOwnedFile(
                    active.carrier,
                    cast("OwnedFileBinding", active.binding),
                    cast("_OwnedFileOutcome", outcome),
                )
            )
        self._release_custody(active, retain_effect=outcome.requires_owner_retention)

    def _capture(self, active: _ActiveFileDownload, outcome: FileDownloadOutcome) -> None:
        active.outcome = outcome
        active.prepared.release_sink()
        if outcome.requires_owner_retention:
            uncertainty = self._uncertainty(
                pending_remote_effects=outcome.pending_remote_effects,
                coordination_uncertain=outcome.coordination_uncertain,
            )
            if outcome.snapshot_ownership_uncertain or outcome.cleanup_debt is not None:
                uncertainty = uncertainty | frozenset({FileCallUncertainty.SCRATCH_OWNERSHIP})
            self._publish_retained(
                active,
                self._obligation(
                    FileCallFamily.DOWNLOAD,
                    active.binding,
                    token=outcome.token,
                    scratch_reference=outcome.ready._reference if outcome.ready is not None else None,
                    scratch_cleanup_debt=outcome.cleanup_debt,
                    uncertainty=uncertainty,
                ),
            )
            self._retain_unfinished(UnfinishedFileDownload(active.carrier, active.binding, outcome))
        self._release_custody(active, retain_effect=outcome.requires_owner_retention)
        self._active_downloads.pop(id(active))

    def _retain_unfinished(self, download: UnfinishedFileDownload) -> None:
        self._unfinished_downloads.append(download)

    def _capture_upload(self, active: _ActiveFileUpload, outcome: FileUploadOutcome) -> None:
        active.outcome = outcome
        if outcome.requires_owner_retention:
            uncertainty = self._upload_uncertainty(outcome)
            self._publish_retained(
                active,
                self._obligation(
                    FileCallFamily.UPLOAD,
                    active.binding,
                    token=outcome.token,
                    scratch_reference=outcome.reference,
                    scratch_cleanup_debt=outcome.scratch_cleanup_debt,
                    publication_cleanup_debt=outcome.publication_cleanup_debt,
                    uncertainty=uncertainty,
                ),
            )
            self._retain_unfinished_upload(UnfinishedFileUpload(active.carrier, active.binding, outcome))
        self._release_custody(active, retain_effect=outcome.requires_owner_retention)
        self._active_uploads.pop(id(active))

    def _retain_unfinished_upload(self, upload: UnfinishedFileUpload) -> None:
        self._unfinished_uploads.append(upload)

    def _capture_json_update(self, active: _ActiveFileJsonUpdate, outcome: FileJsonOutcome) -> None:
        active.outcome = outcome
        if outcome.requires_owner_retention:
            upload = outcome.upload_outcome
            uncertainty = self._uncertainty(
                pending_remote_effects=outcome.pending_remote_effects,
                coordination_uncertain=outcome.coordination_uncertain,
            )
            if upload is not None:
                uncertainty = uncertainty | self._upload_uncertainty(upload)
            self._publish_retained(
                active,
                self._obligation(
                    FileCallFamily.JSON_UPDATE,
                    active.binding,
                    token=upload.token if upload is not None else None,
                    attempt=outcome.publication_attempts if upload is not None else None,
                    scratch_reference=upload.reference if upload is not None else None,
                    scratch_cleanup_debt=upload.scratch_cleanup_debt if upload is not None else None,
                    publication_cleanup_debt=upload.publication_cleanup_debt if upload is not None else None,
                    uncertainty=uncertainty,
                ),
            )
            self._retain_unfinished_json_update(UnfinishedFileJsonUpdate(active.carrier, active.binding, outcome))
        self._release_custody(active, retain_effect=outcome.requires_owner_retention)
        self._active_json_updates.pop(id(active))

    def _retain_unfinished_json_update(self, update: UnfinishedFileJsonUpdate) -> None:
        self._unfinished_json_updates.append(update)
