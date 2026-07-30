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

    def referenced_resources(self) -> list[ResourceReference]:
        """GREENNESS SCAFFOLD (Phase 4 head step): a thin alias for
        :meth:`dependencies` under an empty build context.

        The Phase 4 rename to the uniform ``dependencies(context)`` breaks the
        callers that still invoke edge-extraction by the old name
        (``walk.collect_secrets_for`` and the vm-site / git-credential node
        factories); each migrates to a graph read in its own later sub-step.
        This alias keeps them green in the meantime and is REMOVED at the end
        of Phase 4, once the last caller has moved, before the phase-6 guard
        (which would flag a consumer re-walking ``dependencies``). Non-secret
        resources ignore the context, so the empty context is faithful; a
        secret under an empty context emits only its explicit mapping-key
        edges, which none of the alias callers consume.
        """
        from agentworks.resources.graph import BuildContext

        return self.dependencies(BuildContext())

    def validate(self) -> None:
        """Throwing correctness check for the resource's own capability
        config sub-block(s): the resource-level counterpart of
        ``dependencies`` (the edge-extraction half). Mirrors that
        method's shape, reading ``self``'s fields and delegating to the
        named capability's ``validate``. The finalize ``validate`` pass
        (``Registry.finalize``) invokes it per present node.

        Base behavior: no-op. A resource with no capability config block
        has nothing to validate. Resources that host a capability config
        (``VMSiteDecl``, ``GitCredentialConfig``, ``SessionTemplate``)
        override it.
        """
