"""``DeclaredResource``: the shared base for every declared-resource dataclass.

Every declarable kind carries the same metadata (its ``name``, an optional
operator ``description``, the ``declared_at`` source location, and the
framework's ``origin`` provenance). Concrete resource dataclasses
(``VMTemplate``, ``SecretDecl``, ``VMSiteDecl``, ...) inherit this
base and add only their kind-specific fields, so the metadata exists by
construction rather than being hand-copied per kind. Single-sourcing the
fields here is what keeps a kind from silently lacking one of them (the gap
that let five kinds ship without ``description``).

This base lives in its own top-level module, next to ``source_location`` and
for the same reason it does: the domain resource dataclasses inherit it AT
CLASS-DEFINITION TIME (a runtime dependency, unlike the ``Origin`` type
reference, which ``from __future__ import annotations`` keeps as a string). It
cannot live under ``agentworks.resources``
because importing any submodule of that package runs its ``__init__``, which
eagerly imports every domain kind module (to populate ``KIND_REGISTRY`` via
import side effects), and those kind modules import the very domain dataclasses
that would be inheriting this base, closing a circular import. Homed here, the
base depends only on ``agentworks.source_location`` at runtime and stays lower
than every package that inherits it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.source_location import SourceLocation, synthesized

if TYPE_CHECKING:
    from agentworks.resources.graph import BuildContext
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ResourceReference


@dataclass(frozen=True, kw_only=True)
class DeclaredResource:
    """Common metadata every declared resource carries. Concrete resource
    dataclasses inherit this and add only their kind-specific fields.

    The ``origin`` field defaults to "not yet attached": the framework stamps
    it at publish, and direct-construction call sites (tests, kinds'
    ``synthesize`` paths) get the sentinel for free. ``declared_at`` defaults
    to a synthesized location for the same reason. Inbound references live on
    the dependency graph (``Registry.graph.dependents_of``), not on the
    resource dataclass.
    """

    name: str
    description: str | None = None
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None

    @property
    def error_location(self) -> SourceLocation | None:
        """Where an operator can go to fix a problem with this resource,
        or ``None`` when there is no such place.

        The ORIGIN answers first, because it is the provenance every other
        operator surface renders (``describe``, ``doctor``, and the
        finalize pass's own framing) and it is stamped at publish, by the
        publisher that knows where the row really came from. A row built
        outside that path (a synthesize hook, a test) falls back to its
        own ``declared_at``, and a built-in or auto-declared row has
        neither a file nor a line, so it frames nothing rather than
        pointing at a place that does not exist.
        """
        origin = self.origin
        if origin is not None and origin.file is not None and origin.line:
            return SourceLocation(file=origin.file, line=origin.line)
        return self.declared_at

    def dependencies(self, context: BuildContext) -> list[ResourceReference]:
        """The resource's outbound reference edges: the graph-node
        edge-extraction method the finalize build walk calls per present row.

        The builder threads a :class:`~agentworks.resources.graph.BuildContext`
        (today: the available-backend list a ``secret`` reads to emit its
        ``secret -> secret-backend`` edges); every other resource ignores it.
        Total and non-throwing, like the capability ``dependencies`` it
        composes. Base behavior: no edges.
        """
        return []

    def validate(self, enabled_backends: frozenset[str]) -> None:
        """Throwing correctness check for the resource's own capability
        config sub-block(s): the resource-level counterpart of
        ``dependencies`` (the edge-extraction half). Mirrors that
        method's shape, reading ``self``'s fields and delegating to the
        named capability's ``validate``. The finalize ``validate`` pass
        (``Registry.finalize``) invokes it per present node.

        ``enabled_backends`` is the set of enabled ``secret-backend`` names,
        threaded from the finalize pass (which reads the enablement axis off the
        graph) so a ``secret`` validates only mappings addressed to a present
        AND enabled backend (R9.9). Every non-secret resource ignores it; the
        param is uniform so the pass can call ``validate`` without per-kind
        dispatch.

        Base behavior: no-op. A resource with no capability config block
        has nothing to validate. Resources that host a capability config
        (``VMSiteDecl``, ``GitCredentialConfig``, ``SessionTemplate``)
        override it.
        """
