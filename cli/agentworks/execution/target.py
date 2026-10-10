"""Passive target view supplied by private core composition."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .access import ExecutionAccess, FileAccess
    from .carrier import ChannelFeatures


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    """Existing interfaces and the selected channel's exact feature description.

    Interface absence is a composition decision, not a channel limitation or
    authorization result. Features do not grant actions or report readiness.
    The composition root owns closure; retaining this view does not keep its
    operations open. Supplied interfaces enforce their existing lifetime.
    """

    _execution: ExecutionAccess | None = field(repr=False)
    _files: FileAccess | None = field(repr=False)
    features: ChannelFeatures

    def execution(self) -> ExecutionAccess | None:
        """Return the already-bound execution interface, when supplied."""
        return self._execution

    def files(self) -> FileAccess | None:
        """Return the already-bound file interface, when supplied."""
        return self._files
