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
from agentworks.execution._file_upload import (
    FileUploadBinding,
    FileUploadControlFact,
    FileUploadOutcome,
    _prepare_upload,
    _PreparedUpload,
)

if TYPE_CHECKING:
    from agentworks.execution._file_publication import Create, CreateMetadata, Match, Replace
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution.carrier import ByteSink, ByteSource, Carrier, Deadline
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
        create_metadata: CreateMetadata,
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
        create_metadata: CreateMetadata,
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
