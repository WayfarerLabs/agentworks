"""Value-path provenance primitives shared below schema and resources.

The schema walker emits provenance operations, while the resource layer applies
them to declaration sources. Neither layer owns both responsibilities, so the
operation vocabulary lives here in a stdlib-only leaf that both can import.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

type ProvenancePath = tuple[str | int, ...]


class LayerContributionKind(StrEnum):
    """How one layer changes the provenance retained at a value path."""

    REPLACEMENT = "replacement"
    CONTRIBUTION = "contribution"
    RESET_PREFIX = "reset-prefix"


@dataclass(frozen=True)
class LayerContribution:
    """One provenance update emitted while a layer is merged."""

    path: ProvenancePath
    kind: LayerContributionKind = LayerContributionKind.REPLACEMENT

    @classmethod
    def replacement(cls, *path: str | int) -> LayerContribution:
        """Record the current layer as the sole source at ``path``."""
        return cls(path)

    @classmethod
    def contribution(cls, *path: str | int) -> LayerContribution:
        """Add the current layer as another source at ``path``."""
        return cls(path, LayerContributionKind.CONTRIBUTION)

    @classmethod
    def reset_prefix(cls, *path: str | int) -> LayerContribution:
        """Discard every retained record at or below ``path``."""
        return cls(path, LayerContributionKind.RESET_PREFIX)


def longest_prefix_value[T](values: Mapping[ProvenancePath, T], path: ProvenancePath) -> T | None:
    """Return the value at the longest recorded prefix of ``path``.

    ``None`` means either no prefix is recorded or the longest prefix records
    ``None``. Provenance callers use non-nullable values. Checking prefixes
    from the full path toward the root avoids reading, hashing, or rendering
    authored values outside the normalized path.
    """
    for length in range(len(path), -1, -1):
        prefix = path[:length]
        if prefix in values:
            return values[prefix]
    return None


__all__ = [
    "LayerContribution",
    "LayerContributionKind",
    "ProvenancePath",
    "longest_prefix_value",
]
