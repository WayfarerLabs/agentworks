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

if TYPE_CHECKING:
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution._runtime_prerequisite import RuntimeSelection
    from agentworks.execution.carrier import ByteSink, Carrier, Deadline
    from agentworks.operations import OperationBorrow, OperationOwner


@dataclass(frozen=True, slots=True, repr=False)
class UnfinishedFileDownload:
    """Captured custody for one download with unfinished responsibility."""

    carrier: Carrier = field(repr=False)
    binding: FileDownloadBinding
    outcome: FileDownloadOutcome = field(repr=False)


@dataclass(slots=True, repr=False)
class _ActiveFileDownload:
    carrier: Carrier
    binding: FileDownloadBinding
    borrow: OperationBorrow
    prepared: _PreparedDownload
    outcome: FileDownloadOutcome | None = None


class FileOperation:
    """One core file state shared by all views of an existing owner."""

    def __init__(self, owner: OperationOwner) -> None:
        self._owner = owner
        self._active_downloads: dict[int, _ActiveFileDownload] = {}
        self._unfinished_downloads: list[UnfinishedFileDownload] = []

    @property
    def active_downloads(self) -> tuple[_ActiveFileDownload, ...]:
        return tuple(self._active_downloads.values())

    @property
    def unfinished_downloads(self) -> tuple[UnfinishedFileDownload, ...]:
        return tuple(self._unfinished_downloads)

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
            active = _ActiveFileDownload(carrier, prepared.binding, borrow, prepared)
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

    def _capture(self, active: _ActiveFileDownload, outcome: FileDownloadOutcome) -> None:
        if self._active_downloads.get(id(active)) is not active:
            raise AssertionError("download capture lost its attached working state")
        active.outcome = outcome
        active.prepared.release_sink()
        if outcome.requires_owner_retention:
            self._retain_unfinished(UnfinishedFileDownload(active.carrier, active.binding, outcome))
        active.borrow.close()
        self._forget_active(active)

    def _retain_unfinished(self, download: UnfinishedFileDownload) -> None:
        self._unfinished_downloads.append(download)

    def _forget_active(self, active: _ActiveFileDownload) -> None:
        try:
            removed = self._active_downloads.pop(id(active))
        except KeyError:
            raise AssertionError("download capture lost its attached working state") from None
        if removed is not active:
            raise AssertionError("download capture lost its attached working state")
