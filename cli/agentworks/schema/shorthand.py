"""``ScalarShorthand``: the model an operator may also write as a bare scalar.

Some models have two spellings. An env entry is written ``FOO: a value``
almost everywhere and ``FOO: {secret: my-secret}`` in the handful of
places that need the other form, and the first is the second with one
field filled in.

That is ONE authored fact with three consumers, and every one of them is a
place to be wrong about it:

- **validation** has to accept the scalar, or the shorthand is not a
  spelling at all;
- **emitted JSON Schema** has to offer it as an arm, or a schema-aware
  editor red-underlines the form nearly every operator writes;
- **the field-documentation stream** has to name it, or ``explain``
  and the generated sample tell an operator to write the long form for no
  reason, and ``explain`` is what the resources guide calls the
  authority on what a spec accepts.

Before this class the fact was written twice (a before-validator and a
hand-rolled ``__get_pydantic_json_schema__``, both on ``EnvEntry``) and
the third consumer had no way to learn it, which is exactly the shape the
defect took: the emitted schema offered ``{anyOf: [string, object]}`` for
a field ``explain`` rendered as a table alone. Declared here, all
three derive from the declaration and the walkers can read it off the
class (:func:`scalar_shorthand_of`) without running any of the model's
code.

Internal to the package: the public surface is
``agentworks/schema/__init__``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, get_args, get_origin

from pydantic import Discriminator
from pydantic_core import core_schema

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel, GetCoreSchemaHandler
    from pydantic_core import CoreSchema

#: The scalar types a shorthand may be spelled as, each paired with the
#: pydantic-core schema that validates one.
#:
#: A closed set rather than "any type pydantic can build", because a
#: shorthand's arm has to reach emitted schema through the emitter's OWN
#: generator: ``manifests/emit._ManifestJsonSchema`` widens integers and
#: booleans to the strings a YAML 1.2 editor sees them as, and an arm
#: assembled as a JSON Schema literal here would skip that correction and
#: make the emitted schema stricter than the loader. Building the CORE
#: schema instead and handing it to the active generator is what keeps
#: that correction applied; this mapping is the whole reason the arm can
#: be built that way.
_SCALAR_CORE_SCHEMAS: Final[dict[type, Callable[[], CoreSchema]]] = {
    str: core_schema.str_schema,
    int: core_schema.int_schema,
    float: core_schema.float_schema,
    bool: core_schema.bool_schema,
}


@dataclass(frozen=True, kw_only=True)
class ScalarShorthand:
    """The bare-scalar spelling of a model, and the field it means.

    Declared as a ``ClassVar`` on the model itself
    (:attr:`~agentworks.schema.AgwModel.scalar_shorthand`), because it is
    a fact about the model rather than about any one field that holds one:
    five shipped spec fields hold an ``EnvEntry`` table, and the spelling
    is the same in all five.
    """

    annotation: type
    """The scalar type the shorthand is written as."""

    field: str
    """The model field the scalar's value becomes."""

    def __post_init__(self) -> None:
        if self.annotation not in _SCALAR_CORE_SCHEMAS:
            supported = ", ".join(sorted(scalar.__name__ for scalar in _SCALAR_CORE_SCHEMAS))
            raise StateError(
                f"a scalar shorthand may be spelled as {supported}, not {self.annotation!r}; "
                "a shorthand is one plain value an operator types in a document"
            )
        if not self.field.isidentifier():
            raise StateError(f"a scalar shorthand must name a model field, and {self.field!r} is not a field name")

    def folded(self, data: Any) -> Any:
        """``data`` as the mapping the model validates, or ``data``
        unchanged when it is not the shorthand spelling.

        ``type(...) is`` rather than ``isinstance``: ``bool`` is a
        subclass of ``int``, so an integer shorthand asked with
        ``isinstance`` would fold ``true`` as well, which is the silent
        coercion :class:`~agentworks.schema.AgwModel`'s strict posture
        exists to refuse.
        """
        return {self.field: data} if type(data) is self.annotation else data

    def core_schema(self) -> CoreSchema:
        """The scalar arm, as the schema a JSON Schema generator turns
        into one branch of the emitted ``anyOf``."""
        return _SCALAR_CORE_SCHEMAS[self.annotation]()


@dataclass(frozen=True, kw_only=True)
class UnionScalarShorthand:
    """Dispatch one tagged-union scalar spelling to one declared arm.

    An arm's :class:`ScalarShorthand` says how that MODEL folds a scalar.
    It does not say that a discriminated union can select the arm before
    the tag is available. This field-level declaration supplies that
    missing selection fact once. Validation, schema emission, default
    filling, extraction, and field documentation all read it.

    ``arm`` names the model rather than repeating its scalar type, fold
    field, or tag. Those are derived from the arm's ``ScalarShorthand``
    and its single-string ``Literal`` discriminator field. Registration
    conformance refuses a declaration whose arm is absent, ambiguous, or
    otherwise does not support that derivation.
    """

    discriminator: str
    """The field whose literal value tags the selected arm."""

    arm: type[BaseModel]
    """The union arm selected by the scalar spelling."""

    def __post_init__(self) -> None:
        if not self.discriminator.isidentifier():
            raise StateError(
                f"a union scalar shorthand must name a discriminator field, and "
                f"{self.discriminator!r} is not a field name"
            )

    def __get_pydantic_core_schema__(self, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        """Build ordinary tag dispatch, preceded by this declared fold.

        Invalid declarations deliberately keep ordinary tagged validation
        rather than raising while a plugin module imports. Registration
        conformance reports the author-facing defect before seating the
        plugin, which is the boundary that can name its owner.
        """
        tagged = handler.generate_schema(Annotated[source_type, Discriminator(self.discriminator)])
        resolved = union_scalar_resolution(source_type, self)
        if isinstance(resolved, str):
            return tagged
        shorthand, tag = resolved

        def fold(value: Any) -> Any:
            folded = shorthand.folded(value)
            return value if folded is value else {self.discriminator: tag, **folded}

        return core_schema.no_info_before_validator_function(
            fold,
            tagged,
            json_schema_input_schema=core_schema.union_schema([shorthand.core_schema(), tagged]),
        )


def union_scalar_resolution(
    annotation: object,
    declaration: UnionScalarShorthand,
) -> tuple[ScalarShorthand, str] | str:
    """Resolve ``declaration`` against its union, or explain why not."""
    base = _without_annotated(annotation)
    members = get_args(base) or (base,)
    matches = [member for member in members if _without_annotated(member) is declaration.arm]
    arm_name = getattr(declaration.arm, "__name__", repr(declaration.arm))
    if len(matches) != 1:
        return f"selects arm {arm_name}, which occurs {len(matches)} times in the discriminated union"
    shorthand = scalar_shorthand_of(declaration.arm)
    if shorthand is None:
        return f"selects arm {arm_name}, which declares no ScalarShorthand"
    fields = getattr(declaration.arm, "model_fields", {})
    field = fields.get(declaration.discriminator)
    tags = get_args(field.annotation) if field is not None and get_origin(field.annotation) is Literal else ()
    if len(tags) != 1 or not isinstance(tags[0], str):
        return f"selects arm {arm_name}, whose {declaration.discriminator!r} field is not one string Literal"
    return shorthand, tags[0]


def _without_annotated(annotation: object) -> object:
    """Remove nested ``Annotated`` wrappers from one union member."""
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def scalar_shorthand_of(annotation: object) -> ScalarShorthand | None:
    """The shorthand ``annotation`` declares, when it is a model that
    declares one.

    Takes any annotation rather than a model class, because both callers
    (the classifier widening an annotation, the base folding a value) ask
    the question of something they have not screened yet, and an
    attribute lookup is cheaper than the screen.
    """
    shorthand = getattr(annotation, "scalar_shorthand", None)
    return shorthand if isinstance(shorthand, ScalarShorthand) else None


def shorthand_field_error(model_cls: type[BaseModel]) -> str | None:
    """Why ``model_cls``'s shorthand cannot mean what it says, or ``None``.

    A shorthand folds its scalar into a named field, so the field has to
    exist. It would otherwise fail as a closed-world ``extra_forbidden``
    on a key the operator never wrote, at the moment some operator
    happened to use the shorthand spelling, which is a long way from the
    author who mistyped it.
    """
    shorthand = scalar_shorthand_of(model_cls)
    if shorthand is None or shorthand.field in model_cls.model_fields:
        return None
    declared = ", ".join(model_cls.model_fields) or "none"
    return (
        f"{model_cls.__name__} declares a scalar shorthand folding into {shorthand.field!r}, "
        f"which is not one of its fields ({declared})"
    )
