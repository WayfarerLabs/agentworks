"""The reference field markers and their ``x-agw-ref`` schema encoding.

JSON Schema has no way to say "this string names a secret", so the two
things a field can mean about another Resource are said with
``Annotated`` metadata instead:

.. code-block:: python

    token: Annotated[str, SecretRef(usage="the auth token",
                                    default_template="git-token-{owner_name}")]
    template: Annotated[str, ResourceRef(kind="vm-template", usage="the base image")]

One authored marker feeds every consumer: reference extraction reads it
off ``model_fields``, the boundary fill
(:func:`~agentworks.schema.filled_defaults`) resolves an omitted
templated field from it before validation reads the blob, the
field-reference stream carries it verbatim, and
``__get_pydantic_json_schema__`` puts it into emitted JSON Schema under
a single ``x-agw-ref`` key. There is no second place to keep in sync.

The owner-template vocabulary (``{owner_name}``, ``{owner_kind}``) is
closed and is checked when the MARKER is constructed, so an author's
mistake fails at import of the module declaring the model. That check is
what lets the fill promise it never raises: rendering a validated
template cannot fail.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter
from typing import TYPE_CHECKING, Final

from agentworks.errors import StateError
from agentworks.schema.reference import RefRelationship

if TYPE_CHECKING:
    from pydantic import GetJsonSchemaHandler
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import CoreSchema

#: The whole placeholder vocabulary an owner template may use.
OWNER_PLACEHOLDERS: Final = ("owner_kind", "owner_name")

#: The one JSON Schema extension key the markers emit. The ``x-agw-``
#: prefix is reserved for agentworks vocabulary generally; this is its
#: only member today, and a new member needs a consumer, not just an
#: idea. Conforming validators ignore ``x-`` keys, so editor tooling
#: sees the facts without being confused by them.
REF_SCHEMA_KEY: Final = "x-agw-ref"


@dataclass(frozen=True)
class RefOwner:
    """WHO declared the blob being read: a ``(kind, name)`` address.

    A typed pair rather than the pre-joined ``"git-credential/prod"``
    display string, because owner templates need the NAME and re-splitting
    a string we joined ourselves is exactly the string surgery this
    layer exists to delete. :attr:`display` reproduces that string for
    error framing, so operator-facing text is unchanged.
    """

    kind: str
    name: str
    label: str | None = None
    """An override for :attr:`display`, for a caller that has to frame in a
    narrower vocabulary than ``kind/name``.

    A secret's ``backend_mappings`` value is the standing case: the secret
    alone is ambiguous when it maps several backends, and a root model's
    errors carry no field path of their own, so the owner frames as
    ``secret/npm-token.backend_mappings.onepassword`` (see
    ``SecretDecl._mapping_owner``). Everything else leaves it unset and
    gets ``kind/name``."""

    @property
    def display(self) -> str:
        """The ``"<kind>/<name>"`` form error messages frame with, or
        :attr:`label` when one was given."""
        return self.label or f"{self.kind}/{self.name}"


@dataclass(frozen=True, kw_only=True)
class RefMarker:
    """What a field means when it names another Resource.

    Fields:

    - ``kind``: the target Resource's kind (``"secret"``,
      ``"vm-template"``, ...). Deliberately NOT validated against
      ``KIND_REGISTRY`` here: this package sits below the kind registry
      and must not import it. Registration-time conformance is where a
      bad kind belongs, with the descriptor in hand.
    - ``usage``: prose describing what the referrer needs the target
      for, carried verbatim onto the target's ``ReferenceEntry`` and
      into ``agw resource describe``'s "Referenced by:" section.
      Required, because a marker without it degrades an
      operator-visible surface.
    - ``default_template``: the name to use when the field is omitted,
      as a template over :data:`OWNER_PLACEHOLDERS`. A template with no
      placeholder (``"azure-client-secret"``) is just a constant
      default, so one mechanism covers both shipped shapes.
    - ``relationship``: what the edge MEANS. Carried, not consumed, by
      this package; the traversals that filter on it are later steps'.

    ``kw_only`` throughout so :class:`SecretRef` can default ``kind``
    without the non-default-after-default ordering problem.
    """

    kind: str
    usage: str
    default_template: str | None = None
    relationship: RefRelationship = RefRelationship.USES

    def __post_init__(self) -> None:
        if self.default_template is not None:
            _check_owner_template(self.default_template)

    def render_default(self, owner: RefOwner) -> str | None:
        """This marker's default name for ``owner``, or ``None`` when it
        declares no template.

        Never raises: the template's placeholders were checked at
        construction, and substitution is keyword-only over exactly that
        vocabulary.
        """
        if self.default_template is None:
            return None
        return self.default_template.format(owner_kind=owner.kind, owner_name=owner.name)

    def schema_extension(self) -> dict[str, object]:
        """The ``x-agw-ref`` object this marker contributes to JSON Schema.

        All four keys always appear, the default ``relationship``
        included, so a consumer never has to know our defaults to read
        the schema. Keys are snake_case, matching how agentworks spells
        fields everywhere else, rather than importing JSON Schema's
        camelCase convention into a namespace we own.
        """
        return {
            "kind": self.kind,
            "usage": self.usage,
            "default_template": self.default_template,
            "relationship": self.relationship.value,
        }

    def __get_pydantic_json_schema__(
        self,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """pydantic's native hook for ``Annotated`` metadata: the marker's
        semantics survive into emitted schema from the one authored
        marker, with nothing to keep in sync."""
        json_schema = handler(core_schema)
        json_schema[REF_SCHEMA_KEY] = self.schema_extension()
        return json_schema


@dataclass(frozen=True, kw_only=True)
class ResourceRef(RefMarker):
    """The field names a Resource of a fixed kind."""


@dataclass(frozen=True, kw_only=True)
class SecretRef(RefMarker):
    """The field names a secret, optionally with an owner-templated
    default name (``"git-token-{owner_name}"``)."""

    kind: str = "secret"


def _check_owner_template(template: str) -> None:
    """Reject anything :meth:`RefMarker.render_default` could not render.

    All four of ``Formatter().parse``'s outputs are checked, not just the
    field name: ``"{owner_name:d}"`` has a legal name and still raises at
    ``str.format`` time, and that raise is the one thing reference
    extraction promises cannot happen.
    """
    try:
        parsed = list(Formatter().parse(template))
    except ValueError as exc:
        raise StateError(f"malformed reference default_template {template!r}: {exc}") from None
    known = ", ".join(f"{{{name}}}" for name in OWNER_PLACEHOLDERS)
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name == "" or field_name.isdigit():
            raise StateError(
                f"positional placeholder {{{field_name}}} in reference default_template {template!r}; "
                f"owner templates substitute by name only ({known})"
            )
        if field_name not in OWNER_PLACEHOLDERS:
            raise StateError(
                f"unknown placeholder {{{field_name}}} in reference default_template {template!r}; known: {known}"
            )
        if format_spec:
            raise StateError(
                f"format spec {format_spec!r} on {{{field_name}}} in reference default_template {template!r}; "
                "an owner template substitutes names verbatim"
            )
        if conversion is not None:
            raise StateError(
                f"conversion !{conversion} on {{{field_name}}} in reference default_template {template!r}; "
                "an owner template substitutes names verbatim"
            )
