"""Host-independent contract and errors for private local download staging."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline


class LocalDownloadStage(Protocol):
    """The coordinator's sink, publication, and cleanup custody."""

    @property
    def published(self) -> bool: ...

    @property
    def publication_uncertain(self) -> bool: ...

    @property
    def cleanup_uncertain(self) -> bool: ...

    @property
    def possible_local_change(self) -> bool: ...

    def try_write(self, data: memoryview) -> int: ...

    def commit(self, *, verified_complete: bool, size: int, sha256: str, deadline: Deadline | None = None) -> None: ...

    def abort(self) -> None: ...


class LocalDownloadUnsupportedError(OSError):
    """The host cannot establish the required local publication guarantees."""


class LocalDownloadCleanupError(LocalDownloadUnsupportedError):
    """Local cleanup did not finish; a failed constructor can expose its writer."""

    cleanup_uncertain = False

    def __init__(
        self,
        message: str,
        *,
        unfinished_stage: LocalDownloadStage | None = None,
        setup_error: BaseException | None = None,
        cleanup_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.unfinished_stage = unfinished_stage
        self.setup_error = setup_error
        self.cleanup_error = cleanup_error


class LocalDownloadCleanupUncertainError(LocalDownloadCleanupError):
    """Cleanup has an uncertain close outcome, including failed construction."""

    cleanup_uncertain = True
