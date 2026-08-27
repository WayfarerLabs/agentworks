"""Typed projections for database-backed and pending live resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


LIVE_RESOURCE_KINDS = frozenset({"agent", "console", "session", "vm", "workspace"})
"""Closed database-backed graph vocabulary."""


@dataclass(frozen=True, slots=True)
class LiveResource:
    """One value-free live-resource node published before finalization.

    ``kind`` and ``name`` identify a database-backed resource, or a pending
    candidate in a creating command's prospective build. ``outbound`` references are
    extracted from its typed, fully resolved desired declaration before this
    projection is constructed. The environment target fields retain only graph
    identities needed by the stable secret-usage projection. No declaration
    values are retained.
    """

    kind: str
    name: str
    outbound: tuple[ResourceReference, ...]
    environment_targets: tuple[tuple[str, str], ...] = ()
    admin_environment_targets: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        source = (self.kind, self.name)
        if any(reference.source != source for reference in self.outbound):
            raise ValueError("every live-resource reference must use the live resource as its source")

    def dependencies(self, _context: FinalizeContext) -> Sequence[ResourceReference]:
        """Publish the already-typed desired references to the graph walk."""
        return self.outbound
