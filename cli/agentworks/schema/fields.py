"""``iter_field_docs``: the ordered field-reference stream.

One walker over a model's declared fields, feeding every HUMAN
presentation of it: the generated sample manifest and ``agw resource
describe-kind`` today, and any later prose surface that documents a
field. :class:`FieldDoc` is therefore a shared source rather than a
CLI-layer detail, so widen its shape deliberately, not incidentally.

Machine consumption is the SIBLING derivation, not a consumer of this
stream: emitted JSON Schema comes from ``model_json_schema`` over the
same models, because deriving JSON Schema from this record would mean
writing a second schema generator to keep in sync with pydantic's. One
authored source (the model), two derivations, kept honest by the marker
being the single carrier of reference semantics into both.

The record is presentation-free by rule: it carries the annotation
rather than a rendered string, and no markdown, no ANSI, and no CLI
vocabulary appear anywhere in it. :func:`render_type` is exported
separately so a presenter may adopt our rendering or replace it.
"""

from __future__ import annotations

import inspect
import types
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from agentworks.errors import StateError
from agentworks.schema._shape import (
    Collection,
    element_metadata,
    is_hidden,
    model_fields_of,
    shape_of,
    spine_metadata,
    split_annotated,
    strip_markers,
    unwrap_optional,
)
from agentworks.schema.markers import RefMarker

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from agentworks.schema._shape import FieldShape, UnionArmType

#: A field with no declared default, distinct from a declared default of
#: ``None``.
UNSET: Final = object()

#: The path segment standing for ANY element of a sequence, and the one
#: standing for any key of a table. A model says a list holds tables; it
#: cannot say how many or under what names, so the element's fields are
#: streamed once under a placeholder. These are the only path segments
#: that are not field names, and they are exported so a presenter can
#: recognize one rather than pattern-matching a string.
SEQUENCE_ELEMENT: Final = "[]"
MAPPING_KEY: Final = "<key>"

#: The constraint keys presenters may rely on, normalized to plain names
#: and plain values so nobody has to know that pydantic stores them as
#: ``annotated_types`` objects.
_CONSTRAINT_KEYS: Final = (
    "ge",
    "gt",
    "le",
    "lt",
    "min_length",
    "max_length",
    "multiple_of",
    "pattern",
)

_SCALAR_RENDERINGS: Final[dict[object, str]] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    type(None): "null",
    dict: "table",
    list: "list",
    # What an operator WRITES, not what validation yields: both spellings
    # reach a `datetime` field through a before-validator (see
    # ``declared_resource.Expiry``), so naming the Python class here would
    # tell them to write something YAML has no syntax for. `object` is the
    # same problem in the other direction: it is Python's word for "any
    # value", and it reaches an operator page through a mapping value type
    # (a secret's `backend_mappings`).
    date: "date",
    datetime: "timestamp",
    object: "any",
}


@dataclass(frozen=True, kw_only=True)
class ModelDoc:
    """A model's own identity, for the heading above its fields."""

    model: type[BaseModel]
    title: str
    description: str | None
    """The first paragraph of the class docstring, whitespace collapsed."""


@dataclass(frozen=True, kw_only=True)
class UnionArm:
    """One alternative of a discriminated union field.

    Carries the arm's :class:`ModelDoc` rather than a bare class so a
    presenter can list the alternatives WITH their one-line descriptions
    without digging docstrings out itself.
    """

    tag: str
    doc: ModelDoc


@dataclass(frozen=True, kw_only=True)
class FieldDoc:
    """One declared field, as everything that presents a model needs it."""

    path: tuple[str, ...]
    """The field's address; the leaf name is ``path[-1]``. A presenter
    indents by ``len(path)`` or joins with dots."""

    annotation: object
    """What an operator may WRITE here, as an annotation: the resolved
    declaration with our reference markers stripped, widened by the
    spellings a before-validator accepts and a declaration cannot say (a
    model's scalar shorthand, an owner-templated reference's ``null``).

    The widening is not cosmetic: emitted JSON Schema states the same two
    spellings from the same declarations, and
    ``tests/manifests/test_accepted_type_parity.py`` fails when this
    record offers fewer types than that schema does."""

    required: bool
    default: object
    """The declared default, or :data:`UNSET` when there is none. A
    ``default_factory`` is called to produce it, unless the factory takes
    validated data, which a doc walker does not have."""

    default_template: str | None
    """The owner-templated default name, unrendered: there is no owner at
    this layer. Scalar fields only, which
    :func:`~agentworks.schema.reference_marker_error` enforces at
    registration rather than leaving to authors to observe."""

    description: str | None
    choices: tuple[object, ...]
    """``Literal`` values or ``Enum`` members in declaration order, empty
    when the field is open. Enum members, not their values: a presenter
    that wants the wire form reads ``.value``."""

    constraints: Mapping[str, object]
    """Normalized to plain keys and plain values (``{"min_length": 1}``).

    All from ONE carrier: the field's own spine, or its elements' when the
    spine declares nothing. See :func:`_constraints_of` for why the two are
    never merged."""

    examples: tuple[object, ...]
    """Values the field's author wrote as ``Field(examples=[...])``, in
    declaration order, empty when none were.

    An authored fact about the field, not a rendering of one: each value is
    spelled the way a DOCUMENT carries it (a list stays a list, a table
    stays a mapping), because that is what a presenter has to show and what
    the same declaration puts into emitted JSON Schema. The generated
    sample writes the first one where it has one, which is what makes a
    skeleton teach a real value rather than ``<string>``."""

    ref: RefMarker | None
    """The field's reference semantics, verbatim. For a marked list this
    is the ELEMENT's marker: it is what each element names."""

    key_ref: RefMarker | None
    """A mapping key's reference semantics, carried to its ``<key>`` node."""

    nested_model: type[BaseModel] | None
    """Set when this field opens a nested block, whose fields follow this
    one in the stream at a longer path.

    Set for a field that MAY be a block as well as for one that is: a
    union of scalars and one model (``str | AccountRef``) offers exactly
    one block an operator could write, and the fields it holds are the
    half of it :attr:`annotation` cannot say."""

    item_model: type[BaseModel] | None
    """Set when this field holds MANY blocks (a list of tables, a table
    of tables). The element's fields follow this one in the stream, once,
    under this field's :attr:`item_segment`. Set for a collection whose
    element MAY be a block too, for the reason :attr:`nested_model` is."""

    item_segment: str | None
    """How ONE element of this field is addressed, as the path segment
    standing for it: :data:`SEQUENCE_ELEMENT` for a list, tuple, or set,
    :data:`MAPPING_KEY` for a table, ``None`` when the field holds a
    single value.

    Set for every collection, whatever its elements are. A presenter that
    has to place an element (in a path, in an indent, or as the ``-`` that
    opens a YAML sequence entry) reads it here rather than deciding for
    itself what kind of collection a rendered type string describes."""

    union_arms: tuple[UnionArm, ...]
    """The alternatives, when this field is a discriminated union. Not
    expanded inline: the presenter decides whether to render one arm, all
    of them, or a table, by recursing with ``iter_field_docs``."""

    item_union_arms: tuple[UnionArm, ...]
    """:attr:`union_arms` one level down: the alternatives ONE ELEMENT of
    a collection may be, when the elements are a discriminated union of
    models (``list[Annotated[A | B, Discriminator("kind")]]``).

    Kept apart from :attr:`union_arms` for the reason :attr:`item_model`
    is kept apart from :attr:`nested_model`: one says what the FIELD is,
    the other what one value inside it is. A presenter that read these off
    ``union_arms`` would render the collection itself as one arm, which in
    a document is a whole indent level too shallow.

    Left as handles, and not expanded under a placeholder segment the way
    :attr:`item_model` is, because an arm is a choice rather than a
    structure: the presenter decision ``union_arms`` leaves open is the
    one an element leaves open too."""


def iter_field_docs(model_cls: type[BaseModel]) -> Iterator[FieldDoc]:
    """``model_cls``'s fields in declaration order, nested blocks expanded
    inline depth-first, union arms left as handles (a COLLECTION's element
    arms as much as a field's own).

    A field marked ``SkipJsonSchema`` is not in the stream: it is
    framework surface rather than the operator's, and the same one marker
    takes it out of emitted schema (see
    :func:`agentworks.schema._shape.is_hidden`).

    Determinism is part of the contract: rendered samples have to be
    stable across runs or the tests that pin them are worthless.

    Raises ``StateError`` for a model that cannot be built. That is the
    opposite of what reference extraction does with the same model, and
    deliberately: extraction is total by contract and its caller cannot
    handle an exception, while this walker's caller is a renderer, and a
    silently truncated field reference is worse than a loud failure at
    the moment someone tries to render an unbuildable model.
    """
    yield from _walk(model_cls, (), ())


def model_doc(model_cls: type[BaseModel]) -> ModelDoc:
    """``model_cls``'s own identity: the heading above its fields."""
    title = model_cls.model_config.get("title") or model_cls.__name__
    return ModelDoc(model=model_cls, title=title, description=_class_summary(model_cls))


def render_type(annotation: object) -> str:
    """The operator-facing rendering of a type: "string", "list of
    string", "one of: a, b".

    A presenter may use this or ignore it; nothing in :class:`FieldDoc`
    depends on it.
    """
    # ``strip_markers`` removes OUR markers and keeps every other
    # ``Annotated`` entry, since they are part of what the field is. None of
    # them is a type an operator writes, so the wrapper is dropped here:
    # without this, a constrained field (``Annotated[str,
    # StringConstraints(...)]``) rendered as the word "Annotated", which is
    # the framework's vocabulary leaking into an operator's sample.
    annotation, _metadata = split_annotated(strip_markers(annotation))
    if annotation in _SCALAR_RENDERINGS:
        return _SCALAR_RENDERINGS[annotation]
    if get_origin(annotation) is Literal:
        return "one of: " + ", ".join(_choice_label(value) for value in get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "one of: " + ", ".join(_choice_label(member.value) for member in annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "table"
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (types.UnionType, Union):
        # Deduplicated so a union of two nested models reads as one
        # "table" rather than repeating itself.
        return " or ".join(dict.fromkeys(render_type(arg) for arg in args))
    if origin is list and args:
        return f"list of {render_type(args[0])}"
    if origin is dict and len(args) == 2:
        return f"table of {render_type(args[1])}"
    if origin is not None:
        return _SCALAR_RENDERINGS.get(origin, _name_of(origin))
    return _name_of(annotation)


def _walk(
    model_cls: type[BaseModel],
    path: tuple[str, ...],
    visiting: tuple[type[BaseModel], ...],
) -> Iterator[FieldDoc]:
    """``model_cls``'s fields at ``path``, nested blocks expanded.

    ``visiting`` is the current PATH, not an accumulating visited set: a
    model reachable from itself stops expanding, while two sibling fields
    of the SAME nested model type each expand, which an accumulating set
    would reduce to the first (leaving a generated sample missing a whole
    block an operator has to write).
    """
    fields = model_fields_of(model_cls)
    if fields is None:
        raise StateError(
            f"cannot describe {model_cls.__name__}: the model has unresolved annotations and could not be built"
        )
    visiting = (*visiting, model_cls)
    for name, field in fields.items():
        if is_hidden(field):
            # Framework surface, not the operator's: a declared-resource
            # row carries its provenance beside its spec fields, and a
            # sample that listed ``origin`` would be telling operators to
            # fill in something only the framework sets.
            continue
        shape = shape_of(field)
        yield _field_doc((*path, name), field, shape)
        block, segment = _expandable(shape)
        if block is not None and block not in visiting:
            yield from _walk(block, (*path, name, *segment), visiting)


def _expandable(shape: FieldShape) -> tuple[type[BaseModel] | None, tuple[str, ...]]:
    """The model whose fields follow this one in the stream, and the path
    segment they hang under.

    A nested block hangs directly under its field name. A collection's
    elements hang under a placeholder, because the model says a list
    holds tables without saying how many: rendering the element ONCE is
    what makes a generated sample complete, and leaving it out is what
    made FR10's "complete skeleton" promise false for a catalog field.

    A collection whose elements are a TAGGED union opens nothing here.
    Its element is a choice among models rather than one model, so the
    arms ride on the doc (``FieldDoc.item_union_arms``) and the presenter
    picks, exactly as it does for a union written on the field itself.

    A union of scalars and ONE model does open its block, at both depths:
    there is nothing to pick, the block is the only shape with fields, and
    the scalar arms are already in the rendered type. The stream's own
    ``visiting`` guard covers it, so a model reachable from itself through
    such a union terminates like any other nested block.
    """
    if shape.block is not None:
        return shape.block, ()
    if shape.item_block is not None and (segment := _segment_of(shape)) is not None:
        return shape.item_block, (segment,)
    return None, ()


def _segment_of(shape: FieldShape) -> str | None:
    """The path segment standing for ONE element of this field, or
    ``None`` when the field holds a single value."""
    if shape.collection is None:
        return None
    return MAPPING_KEY if shape.collection is Collection.MAPPING else SEQUENCE_ELEMENT


def _field_doc(path: tuple[str, ...], field: FieldInfo, shape: FieldShape) -> FieldDoc:
    marker = shape.marker or shape.item_marker
    return FieldDoc(
        path=path,
        annotation=shape.annotation,
        required=field.is_required(),
        default=_default_of(field),
        default_template=shape.marker.default_template if shape.marker else None,
        description=field.description,
        choices=_choices_of(shape.annotation),
        constraints=_constraints_of(field),
        examples=tuple(field.examples or ()),
        ref=marker,
        key_ref=shape.mapping_key_marker,
        # The block a field OPENS, whether it always is one or only may
        # be, from the same pairing ``_expandable`` walks.
        nested_model=shape.block,
        item_model=shape.item_block,
        item_segment=_segment_of(shape),
        union_arms=_documented(shape.arms),
        item_union_arms=_documented(shape.item_arms),
    )


def _documented(arms: tuple[UnionArmType, ...]) -> tuple[UnionArm, ...]:
    """Classified arms, each carrying the identity a presenter lists it
    by. One conversion for both depths, so a field's arms and its
    elements' cannot come to different answers about the same model."""
    return tuple(UnionArm(tag=arm.tag, doc=model_doc(arm.model)) for arm in arms)


def _default_of(field: FieldInfo) -> object:
    if field.is_required():
        return UNSET
    if field.default_factory is None:
        return field.default
    if field.default_factory_takes_validated_data:
        # The factory needs the other fields' validated values, which a
        # doc walker has none of. Reporting no default beats inventing
        # one out of an empty document.
        return UNSET
    return field.get_default(call_default_factory=True)


def _choices_of(annotation: object) -> tuple[object, ...]:
    annotation = _peeled(annotation)
    if get_origin(annotation) is Literal:
        return get_args(annotation)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return tuple(annotation)
    return ()


def _peeled(annotation: object) -> object:
    """``annotation`` with its ``Annotated`` wrappers and its ``| None``
    arms removed, however the two nest.

    Both peels are needed at both depths, because pydantic keeps whichever
    spelling the author used: ``Annotated[Literal[...] | None, Field(...)]``
    hides the union inside the wrapper and
    ``Annotated[Literal[...], Field(...)] | None`` hides the wrapper inside
    the union. Peeling only one of the two reports an OPEN field for a
    closed one, which reaches an operator as a sample with no values in it
    and a describe line that lists none: a wrong answer with nothing to
    signal it.

    Terminates because each peel strictly reduces nesting, and both
    helpers return their argument unchanged when there is nothing left to
    remove.
    """
    while True:
        base, _metadata = split_annotated(annotation)
        base, _optional = unwrap_optional(base)
        if base is annotation:
            return base
        annotation = base


def _constraints_of(field: FieldInfo) -> Mapping[str, object]:
    """The field's constraints, from ONE carrier: its own spine when that
    declares any, otherwise its elements'.

    The same precedence ``ref`` uses (``shape.marker or shape.item_marker``),
    and for the same reason: what the author spelled on the field wins, and
    the elements answer for a field that says nothing itself, which is how
    every shipped collection is written.

    One carrier, never both merged. A list's ``min_length`` bounds how many
    entries it holds and a string element's bounds how long one entry is;
    flattened into a single mapping, the two arrive at
    ``describe``/``sample`` spelled identically and an operator reads a
    limit on the wrong thing. Reporting one carrier's facts can omit
    something; merging them states something false.
    """
    spine = _constraints_in(spine_metadata(field))
    return MappingProxyType(spine or _constraints_in(element_metadata(field)))


def _constraints_in(metadata: list[object]) -> dict[str, object]:
    """The normalized constraints one metadata list carries."""
    constraints: dict[str, object] = {}
    for item in _constraint_carriers(metadata):
        if isinstance(item, RefMarker):
            continue
        for key in _CONSTRAINT_KEYS:
            value = getattr(item, key, None)
            if value is not None:
                constraints[key] = value
    return constraints


def _constraint_carriers(metadata: list[object]) -> Iterator[object]:
    """Every metadata object that could carry a constraint.

    A ``Field(...)`` written inside an ``Annotated`` (rather than as the
    assigned default) survives as a nested ``FieldInfo`` holding its own
    metadata list, so that one level is flattened here.
    """
    for item in metadata:
        if isinstance(item, FieldInfo):
            yield from item.metadata
        else:
            yield item


def _class_summary(model_cls: type[BaseModel]) -> str | None:
    """The first paragraph of the class's own docstring, whitespace
    collapsed. ``__doc__`` is read off the class itself so a model with
    no docstring reports none rather than inheriting its base's."""
    doc = model_cls.__dict__.get("__doc__")
    if not isinstance(doc, str) or not doc.strip():
        return None
    first = inspect.cleandoc(doc).split("\n\n", 1)[0]
    return " ".join(first.split())


def _choice_label(value: object) -> str:
    """One closed-field value as a DOCUMENT spells it.

    ``str()`` is Python's spelling, and for the two literals whose
    spellings differ it is the wrong one: a secret's opt-out is written
    ``false`` in YAML, and telling an operator "one of: False" hands them a
    value the loader rejects.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return "null" if value is None else str(value)


def _name_of(annotation: object) -> str:
    name = getattr(annotation, "__name__", None)
    return name if isinstance(name, str) else str(annotation)
