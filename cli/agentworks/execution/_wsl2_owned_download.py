"""Private ordinary WSL2 hold, target, and DOWNLOAD composition."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from agentworks.execution._file_download import FileDownloadOutcome, FileDownloadStatus
from agentworks.execution._file_gate_setup import FileEffectGateSetup
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._wsl2_owned_operation import WSL2OwnedOperation
from agentworks.vms.target_preparation import VMTargetPreparationStatus

if TYPE_CHECKING:
    from agentworks.execution._helper_launcher import IdentityPlan
    from agentworks.execution.carrier import ByteSink, Deadline


class WSL2DownloadStatus(StrEnum):
    COMPLETE = "complete"
    REFUSED = "refused"
    RETAINED = "retained"


class WSL2OwnedDownload(WSL2OwnedOperation):
    """One caller-retained private download; escaping control flow keeps its claim."""

    _purpose = "download"
    file_operation: FileOperation | None = None
    outcome: FileDownloadOutcome | None = None

    def download(
        self,
        *,
        trusted_root_path: str,
        relative_path: str,
        sink: ByteSink,
        max_bytes: int,
        plan: IdentityPlan,
        deadline: Deadline,
    ) -> WSL2DownloadStatus:
        """Run once; only typed settled obligations permit whole-owner release."""
        guest = self.start_and_prepare(deadline)
        if self.preparation is None:
            return WSL2DownloadStatus.RETAINED
        if self.preparation.status is not VMTargetPreparationStatus.PREPARED:
            return self._release_status(deadline, safe=not self.preparation.requires_owner_retention)
        if guest is None:
            return self._release_status(deadline, safe=True)

        target = self.preparation.target
        assert target is not None
        gate_setup = FileEffectGateSetup.for_target(target, plan.expected.euid, guest)
        self.file_operation = FileOperation(self.owner, target)
        self.outcome = self.file_operation.download(
            self._carrier,
            trusted_root_path=trusted_root_path,
            relative_path=relative_path,
            sink=sink,
            max_bytes=max_bytes,
            plan=plan,
            deadline=deadline,
            runtime_selection=self._runtime,
            gate_setup=gate_setup,
        )
        file_settled = (
            not self.outcome.requires_owner_retention
            and not self.file_operation.active_downloads
            and not self.file_operation.unfinished_downloads
        )
        return self._release_status(deadline, safe=file_settled)

    def _release_status(self, deadline: Deadline, *, safe: bool) -> WSL2DownloadStatus:
        if not self.release_if_settled(deadline, safe=safe):
            return WSL2DownloadStatus.RETAINED
        if self.outcome is None or self.outcome.status is not FileDownloadStatus.COMPLETE:
            return WSL2DownloadStatus.REFUSED
        return WSL2DownloadStatus.COMPLETE
