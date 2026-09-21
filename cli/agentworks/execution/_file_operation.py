"""Core-owned state for concrete private file calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from agentworks.execution._file_inventory_exchange import FileInventoryCandidateResult
    from agentworks.execution._file_metadata_exchange import FileMetadataCandidateResult
    from agentworks.execution._file_object_exchange import FileObjectCandidateResult
    from agentworks.execution._file_objects import FileKind
    from agentworks.execution._file_publication import Create, Match, Replace
    from agentworks.execution._file_stat import FileRevision
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
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
    outcome: OutcomeT | None = None


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

    def __init__(self, owner: OperationOwner) -> None:
        self._owner = owner
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
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    active.outcome = fact.outcome
                    if fact.outcome.requires_owner_retention:
                        self._unfinished_owned_files.append(
                            UnfinishedOwnedFile(active.carrier, active.binding, fact.outcome)
                        )
                    active.borrow.close()
                    self._active_stats.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        active.outcome = outcome
        if outcome.requires_owner_retention:
            self._unfinished_owned_files.append(UnfinishedOwnedFile(active.carrier, active.binding, outcome))
        active.borrow.close()
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
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    active.outcome = fact.outcome
                    if fact.outcome.requires_owner_retention:
                        self._unfinished_owned_files.append(
                            UnfinishedOwnedFile(active.carrier, active.binding, fact.outcome)
                        )
                    active.borrow.close()
                    self._active_inventories.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        active.outcome = outcome
        if outcome.requires_owner_retention:
            self._unfinished_owned_files.append(UnfinishedOwnedFile(active.carrier, active.binding, outcome))
        active.borrow.close()
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
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    active.outcome = fact.outcome
                    if fact.outcome.requires_owner_retention:
                        self._unfinished_owned_files.append(
                            UnfinishedOwnedFile(active.carrier, active.binding, fact.outcome)
                        )
                    active.borrow.close()
                    self._active_removals.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        active.outcome = outcome
        if outcome.requires_owner_retention:
            self._unfinished_owned_files.append(UnfinishedOwnedFile(active.carrier, active.binding, outcome))
        active.borrow.close()
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
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    active.outcome = fact.outcome
                    if fact.outcome.requires_owner_retention:
                        self._unfinished_owned_files.append(
                            UnfinishedOwnedFile(active.carrier, active.binding, fact.outcome)
                        )
                    active.borrow.close()
                    self._active_metadata.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        active.outcome = outcome
        if outcome.requires_owner_retention:
            self._unfinished_owned_files.append(UnfinishedOwnedFile(active.carrier, active.binding, outcome))
        active.borrow.close()
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
        try:
            outcome = prepared.run()
        except BaseException as control:
            fact = control.__cause__
            if isinstance(fact, OwnedFileControlFact):
                try:
                    active.outcome = fact.outcome
                    if fact.outcome.requires_owner_retention:
                        self._unfinished_owned_files.append(
                            UnfinishedOwnedFile(active.carrier, active.binding, fact.outcome)
                        )
                    active.borrow.close()
                    self._active_metadata.pop(id(active))
                except BaseException:
                    raise control from fact
            raise
        active.outcome = outcome
        if outcome.requires_owner_retention:
            self._unfinished_owned_files.append(UnfinishedOwnedFile(active.carrier, active.binding, outcome))
        active.borrow.close()
        self._active_metadata.pop(id(active))
        return outcome

    def _capture(self, active: _ActiveFileDownload, outcome: FileDownloadOutcome) -> None:
        active.outcome = outcome
        active.prepared.release_sink()
        if outcome.requires_owner_retention:
            self._retain_unfinished(UnfinishedFileDownload(active.carrier, active.binding, outcome))
        active.borrow.close()
        self._active_downloads.pop(id(active))

    def _retain_unfinished(self, download: UnfinishedFileDownload) -> None:
        self._unfinished_downloads.append(download)

    def _capture_upload(self, active: _ActiveFileUpload, outcome: FileUploadOutcome) -> None:
        active.outcome = outcome
        if outcome.requires_owner_retention:
            self._retain_unfinished_upload(UnfinishedFileUpload(active.carrier, active.binding, outcome))
        active.borrow.close()
        self._active_uploads.pop(id(active))

    def _retain_unfinished_upload(self, upload: UnfinishedFileUpload) -> None:
        self._unfinished_uploads.append(upload)

    def _capture_json_update(self, active: _ActiveFileJsonUpdate, outcome: FileJsonOutcome) -> None:
        active.outcome = outcome
        if outcome.requires_owner_retention:
            self._retain_unfinished_json_update(UnfinishedFileJsonUpdate(active.carrier, active.binding, outcome))
        active.borrow.close()
        self._active_json_updates.pop(id(active))

    def _retain_unfinished_json_update(self, update: UnfinishedFileJsonUpdate) -> None:
        self._unfinished_json_updates.append(update)
