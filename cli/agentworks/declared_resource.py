"""``DeclaredResource``: the shared base every declared-resource row extends.

Every declarable kind carries the same metadata (its ``name``, an optional
operator ``description``, the ``declared_at`` source location, and the
framework's ``origin`` provenance). Concrete resource rows
(``VMTemplate``, ``SecretDecl``, ``VMSiteDecl``, ...) inherit this
base and add only their kind-specific fields, so the metadata exists by
construction rather than being hand-copied per kind. Single-sourcing the
fields here is what keeps a kind from silently lacking one of them (the gap
that let five kinds ship without ``description``).

**The row IS the kind's spec model, and the metadata / spec split is what
lets one class be both.** A row's operator-writable fields are what a
manifest's ``spec`` may say; the two framework fields below carry
``SkipJsonSchema``, which takes them out of emitted JSON Schema (pydantic's
own behavior) and out of the field-reference stream every human
presentation reads. So the emission surface is ``model_json_schema(row)``
and the render surface is ``iter_field_docs(row)``, both with no filtering
at the call site. The metadata fields are still FIELDS, which is why decode
refuses one written inside ``spec``: it would be accepted and would
silently override the envelope.

This base lives in its own top-level module, next to ``source_location``
and ``origin`` and for the same reason they do: the domain rows inherit it
AT CLASS-DEFINITION TIME, and a model resolves its field annotations then
too. It cannot live under ``agentworks.resources``
because importing any submodule of that package runs its ``__init__``, which
eagerly imports every domain kind module (to populate ``KIND_REGISTRY`` via
import side effects), and those kind modules import the very domain rows
that would be inheriting this base, closing a circular import. Homed here,
the base depends only on ``agentworks.source_location``,
``agentworks.origin``, and ``agentworks.schema`` at runtime, and stays
lower than every package that inherits it.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import AfterValidator, BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from agentworks.errors import StateError
from agentworks.origin import Origin
from agentworks.schema import AgwModel
from agentworks.source_location import SourceLocation, synthesized

if TYPE_CHECKING:
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


class DeclaredResource(AgwModel):
    """Common metadata every declared resource carries. Concrete resource
    rows inherit this and add only their kind-specific fields.

    The ``origin`` field defaults to "not yet attached": the framework stamps
    it at publish, and direct-construction call sites (tests, kinds'
    ``synthesize`` paths) get the sentinel for free. ``declared_at`` defaults
    to a synthesized location for the same reason. Inbound references live on
    the dependency graph (``Registry.graph.dependents_of``), not on the
    resource row.

    ``declared_at`` and ``origin`` keep their frozen-dataclass types rather
    than becoming nested models: a strict model accepts an already-built
    frozen dataclass for such a field and REFUSES a mapping, which is
    exactly the right answer for a field only the framework sets.
    """

    name: str
    description: str | None = None
    declared_at: SkipJsonSchema[SourceLocation] = Field(default_factory=synthesized)
    origin: SkipJsonSchema[Origin | None] = None

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

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        """The resource's outbound reference edges: the graph-node
        edge-extraction method the finalize build walk calls per present row.

        The builder threads a :class:`~agentworks.resources.graph.FinalizeContext`
        (the available-backend list a ``secret`` reads to emit its
        ``secret -> secret-backend`` edges, and the published rows an
        inheriting resource resolves its chain over); every other resource
        ignores it. Total and non-throwing, like the capability
        ``dependencies`` it composes. Base behavior: no edges.
        """
        return []

    def validate_config(self, enabled_backends: frozenset[str], context: FinalizeContext) -> None:
        """Throwing correctness check for the resource's own capability
        config sub-block(s): the resource-level counterpart of
        ``dependencies`` (the edge-extraction half). Mirrors that
        method's shape, reading ``self``'s fields and handing the blob to
        the core's ``validate_capability_config``. The finalize validate
        pass (``Registry.finalize``) invokes it per present node.

        Named ``validate_config`` rather than ``validate``, which is what
        it was called while the rows were dataclasses: ``BaseModel`` already
        has a (deprecated) ``validate`` classmethod meaning something else
        entirely, so the old name would resolve on EVERY row rather than on
        the three that define this, and the finalize pass's
        ``getattr(resource, ...)`` lookup would call pydantic's with this
        method's arguments.

        ``enabled_backends`` is the set of enabled ``secret-backend`` names,
        threaded from the finalize pass (which reads the enablement axis off the
        graph) so a ``secret`` validates only mappings addressed to a present
        AND enabled backend (R9.9). Every non-secret resource ignores it; the
        param is uniform so the pass can call it without per-kind
        dispatch.

        ``context`` is the same :class:`~agentworks.resources.graph.FinalizeContext`
        the build walk was handed, so an INHERITING resource validates the
        merged declaration its edges came from rather than a partial
        declared blob (FR12). It stays separate from ``enabled_backends``
        rather than absorbing it: the enabled-backend set is only known
        after the fold, so a context field for it would read empty during
        the build walk, and a field that is silently wrong at one call site
        is worse than a second parameter.

        Base behavior: no-op. A resource with no capability config block
        has nothing to validate. Resources that host a capability config
        (``VMSiteDecl``, ``GitCredentialConfig``, ``SessionTemplate``)
        override it.
        """


def replace_fields(row: Any, **updates: Any) -> Any:
    """``row`` with ``updates`` applied, for a frozen dataclass or a
    frozen model.

    Declared-resource rows are models and the capability marker rows
    (``VMPlatformEntry`` and kin) are still frozen dataclasses, and both
    flow through the same framework code (origin stamping, the migrator's
    source-field normalization). One helper rather than an
    ``is_dataclass`` branch at each site, because a branch that silently
    no-ops on the shape it does not know is exactly how the migrator's
    equivalence check would start comparing provenance.

    Framework-supplied values only: the model path does not re-validate,
    exactly as ``dataclasses.replace`` does not.
    """
    if dataclasses.is_dataclass(row) and not isinstance(row, type):
        return dataclasses.replace(row, **updates)
    if isinstance(row, BaseModel):
        return row.model_copy(update=updates)
    raise StateError(f"cannot replace fields on {type(row).__name__}: it is neither a frozen dataclass nor a model")


def ResourceName(max_length: int) -> object:  # noqa: N802  (an annotation factory, named like the type it builds)
    """The ``name`` annotation for a kind whose names are validated at
    load, with that kind's cap.

    The cap is never defaulted here: there is no single correct ceiling,
    and each kind's is derived at the module that owns its sink (see
    ``validate_name``). A kind that does not validate its names at load
    keeps the base's plain ``str``, exactly as it does today.

    The wrapper converts the agentworks ``ValidationError`` into a
    ``ValueError`` because the former is NOT a ``ValueError`` subclass
    (it extends ``AgentworksError``), and pydantic re-raises an exception
    that is neither, so it would escape ``model_validate`` and bypass the
    error bridge entirely, losing the batch framing for that one error
    class.
    """

    def _check(value: str) -> str:
        # Imported inside the validator, not at module scope: importing
        # ``agentworks.config.validation`` runs the config package, which
        # imports the very domain modules that declare the rows using
        # this annotation.
        from agentworks.config.validation import validate_name
        from agentworks.errors import ValidationError

        try:
            validate_name(value, max_length=max_length)
        except ValidationError as exc:
            raise ValueError(str(exc)) from None
        return value

    return Annotated[str, AfterValidator(_check)]
