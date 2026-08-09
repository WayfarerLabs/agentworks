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
lets one class be both.** A row carries three kinds of field:

- the kind's own SPEC fields, which is what a manifest's ``spec`` may say;
- the ENVELOPE fields (``EnvelopeMetadata`` below: ``name``,
  ``description``, ``expires``), which the operator writes in the
  document's ``metadata`` block;
- the FRAMEWORK fields (``declared_at``, ``origin``), which nothing
  outside the framework sets.

Only the first group is spec surface, and ``SkipJsonSchema`` on the other
two is what says so, once, for both derivations: pydantic drops such a
field from ``model_json_schema`` natively, and ``iter_field_docs`` drops
it from the field-reference stream every human presentation reads. So the
emission surface is ``model_json_schema(row)`` and the render surface is
``iter_field_docs(row)``, both with no filtering at the call site.

The envelope and framework fields are still FIELDS, which is why decode
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
from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Final

from pydantic import BaseModel, BeforeValidator, Field
from pydantic.json_schema import SkipJsonSchema

from agentworks.errors import StateError
from agentworks.origin import Origin
from agentworks.schema import AgwModel, marker_of
from agentworks.source_location import SourceLocation, synthesized

if TYPE_CHECKING:
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.reference import ResourceReference


def _an_expiry_spelling(value: Any) -> Any:
    """Refuse everything but the three spellings a YAML document can
    produce for a moment in time.

    ``expires`` is one of the base model's sanctioned per-field
    carve-outs: pyyaml's safe loader yields a ``datetime`` for
    ``2026-01-01T00:00:00Z``, a ``date`` for ``2026-01-01``, and a ``str``
    for ``"2026-01-01"``, and strict mode accepts only the first. Lax mode
    accepts all three, and one thing more: a bare ``int``, which it reads
    as a unix timestamp, so ``expires: 12`` would validate to 1970. That
    is nonsense as an expiry, so the carve-out widens the accepted
    SPELLINGS and never the accepted types.
    """
    if isinstance(value, str | date | datetime):
        return value
    raise ValueError("must be a date or an RFC 3339 timestamp")


Expiry = Annotated[
    datetime,
    Field(strict=False),
    BeforeValidator(_an_expiry_spelling, json_schema_input_type=str | date | datetime),
]
"""When a declared resource stops being valid.

This effort models and validates the field only; acting on expiry is a
separate effort (issue #170), so nothing reads it yet.

``json_schema_input_type`` names what the validator ACCEPTS, because a
before-validator otherwise emits the schema of what it produces: a bare
``format: date-time``, which an editor asserting formats would
red-underline for the ``2026-01-01`` this field accepts. Emitted schema
has to describe the document an operator writes, not the object
validation yields."""


class EnvelopeMetadata(AgwModel):
    """The row fields an operator writes in a document's ``metadata``
    block rather than in its ``spec``.

    A separate base, not a marker and not a hand-kept list: the envelope
    derives the metadata keys it accepts from :data:`METADATA_FIELDS`,
    which is this class's own field set, so a fourth metadata field cannot
    be accepted by one layer and rejected by the other. Every field here
    carries ``SkipJsonSchema``, which is what keeps it out of the SPEC
    surface (emitted schema, the field-reference stream, and the
    unknown-key message's list of what is valid); ``spec.name`` is refused
    by decode, so offering it as a spec field would be a lie.

    A kind that RE-DECLARES one of these (``secret`` makes ``description``
    required, ``admin-template`` defaults ``name``) must carry the marker
    on the override too, or the field re-enters that kind's spec surface.
    ``tests/manifests/test_kind_models.py`` pins that for every kind.
    """

    name: SkipJsonSchema[str]
    """The resource name used in ``kind/name`` references. ``/`` is not
    allowed. Prefer lowercase letters and digits with internal hyphens or
    underscores."""

    description: SkipJsonSchema[str | None] = None
    """One operator-facing line saying what this resource is for, shown by
    `agw resource list` and `agw resource describe`."""

    expires: SkipJsonSchema[Expiry | None] = None
    """When this resource stops being valid: a date (`2026-01-01`) or an
    RFC 3339 timestamp. Recorded and validated; nothing acts on it yet."""


#: The metadata keys a manifest document may carry, derived from the base
#: that declares them.
METADATA_FIELDS: Final = frozenset(EnvelopeMetadata.model_fields)


class DeclaredResource(EnvelopeMetadata):
    """Common metadata every declared resource carries. Concrete resource
    rows inherit this and add only their kind-specific fields.

    The ``origin`` field defaults to "not yet attached": the framework stamps
    it at publish, and direct-construction call sites (tests, kinds'
    ``synthesize`` paths) get the sentinel for free. ``declared_at`` defaults
    to a synthesized location for the same reason. Inbound references live on
    the dependency graph (``Registry.graph.dependents_of``), not on the
    resource row.

    These two are framework surface rather than envelope surface: no
    document may set either. They keep their frozen-dataclass types rather
    than becoming nested models, because a strict model accepts an
    already-built frozen dataclass for such a field and REFUSES a mapping,
    which is exactly the right answer for a field only the framework sets.
    """

    declared_at: SkipJsonSchema[SourceLocation] = Field(default_factory=synthesized)
    origin: SkipJsonSchema[Origin | None] = None

    NAME_MAX_LENGTH: ClassVar[int | None] = None
    """The cap this kind's names are checked against AT DECODE, or ``None``
    when the kind does not check its names at all (which is every kind but
    ``secret`` and ``vm-site``).

    Declared as data the decoder reads rather than as a validator on the
    ``name`` field, and the distinction is load-bearing: only a name an
    OPERATOR wrote is checked. Auto-declared and synthesized rows carry
    whatever name the reference that summoned them used, and the shipped
    decision for issue #279 is that those stay tolerant, so a
    non-conforming reference still declares and resolves rather than
    sinking the config. A field validator would fire on every
    construction and break exactly that.

    The cap is never defaulted here: each kind's ceiling is derived at the
    module that owns its sink (see ``agentworks.naming.validate_name``).
    """

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Refuse an owner-templated default on an INHERITING kind's row.

        The boundary fill (:func:`~agentworks.schema.filled_defaults`)
        fills any field whose marker declares a ``default_template`` and
        whose value is absent or ``None``. On a capability config model
        that is exactly right, because the
        validated blob IS the effective blob. On a row that composes along
        an ``inherits`` chain it is wrong, and silently so: ``None`` there
        means "inherit", so filling it would give every template the
        literal default, a child that inherits its parent's override would
        stop doing so, and every template in the config would declare an
        edge to the default resource.

        So a kind spec's reference markers carry ``kind``, ``usage`` and
        ``relationship``, never ``default_template``; an inheriting kind's
        default belongs to the RESOLVED layer, which this base does not
        model. Enforced here rather than written down, because the next
        author needs to hit it at import of their own module rather than
        read this docstring. Keyed on the ``inherits`` field, so a kind
        that GAINS inheritance inherits the rule with it.
        """
        super().__pydantic_init_subclass__(**kwargs)
        if "inherits" not in cls.model_fields:
            return
        offenders = sorted(
            name
            for name, field in cls.model_fields.items()
            if (marker := marker_of(field)) is not None and marker.default_template is not None
        )
        if offenders:
            raise StateError(
                f"{cls.__name__} composes along an `inherits` chain, so its reference markers may not "
                f"declare a default_template; {', '.join(offenders)} does. Filling an absent field would "
                f"make `None` stop meaning `inherit`, so a child that overrides its parent's value would "
                f"silently keep the framework's default instead. Put the default on the resolved layer."
            )

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

    def validate_config(self, context: FinalizeContext) -> None:
        """Throwing correctness check for the resource's own capability
        config sub-block(s): the resource-level counterpart of
        ``dependencies`` (the edge-extraction half). Mirrors that
        method's shape, reading ``self``'s fields and handing the blob to
        the core's ``validate_capability_config``. The finalize validate
        pass (``Registry.finalize``) invokes it per present node.

        The signature carries no enablement input: what config is valid is
        the declared model's answer alone. Invalid configuration cannot be
        banked behind a disabled capability.

        ``context`` is the same :class:`~agentworks.resources.graph.FinalizeContext`
        the build walk was handed, so an INHERITING resource validates the
        merged declaration its edges came from rather than a partial
        declared blob (FR12).

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
    flow through the same framework code (origin stamping, the auto-declared
    description). One helper rather than an ``is_dataclass`` branch at each
    site, because a branch that silently no-ops on the shape it does not
    know would leave a row unstamped and nothing would say so: the caller
    gets an object back either way.

    Framework-supplied values only: the model path does not re-validate,
    exactly as ``dataclasses.replace`` does not.
    """
    if dataclasses.is_dataclass(row) and not isinstance(row, type):
        return dataclasses.replace(row, **updates)
    if isinstance(row, BaseModel):
        return row.model_copy(update=updates)
    raise StateError(f"cannot replace fields on {type(row).__name__}: it is neither a frozen dataclass nor a model")
