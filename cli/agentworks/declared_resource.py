"""``DeclaredResource``: the shared base for every declared-resource dataclass.

Every declarable kind carries the same metadata (its ``name``, an optional
operator ``description``, the ``declared_at`` source location, the framework's
``origin`` provenance, and the inbound ``references`` list). Concrete resource
dataclasses (``VMTemplate``, ``SecretDecl``, ``VMSiteDecl``, ...) inherit this
base and add only their kind-specific fields, so the metadata exists by
construction rather than being hand-copied per kind. Single-sourcing the
fields here is what keeps a kind from silently lacking one of them (the gap
that let five kinds ship without ``description``).

This base lives in its own top-level module, next to ``source_location`` and
for the same reason it does: the domain resource dataclasses inherit it AT
CLASS-DEFINITION TIME (a runtime dependency, unlike the ``Origin`` /
``ReferenceEntry`` type references, which ``from __future__ import
annotations`` keeps as strings). It cannot live under ``agentworks.resources``
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
    from agentworks.resources.origin import Origin
    from agentworks.resources.reference import ReferenceEntry, ResourceReference


@dataclass(frozen=True, kw_only=True)
class DeclaredResource:
    """Common metadata every declared resource carries. Concrete resource
    dataclasses inherit this and add only their kind-specific fields.

    The registry-layer fields (``origin``, ``references``) default to "not yet
    attached": the framework stamps ``origin`` at publish and ``references``
    at ``finalize``, and direct-construction call sites (tests, kinds'
    ``synthesize`` paths) get the sentinels for free. ``declared_at`` defaults
    to a synthesized location for the same reason.
    """

    name: str
    description: str | None = None
    declared_at: SourceLocation = field(default_factory=synthesized)
    origin: Origin | None = None
    references: tuple[ReferenceEntry, ...] = ()

    def referenced_resources(self) -> list[ResourceReference]:
        return []
