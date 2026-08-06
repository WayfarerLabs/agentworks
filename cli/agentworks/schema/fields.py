"""``iter_field_docs``: the ordered field-reference stream.

One walker over a model's declared fields, feeding every HUMAN
presentation of it: the generated config sample, ``agw resource
describe``, and the roadmap's ``agw guide`` topic pages, which the
onboarding effort owns. That last one makes :class:`FieldDoc` a shared
source rather than a CLI-layer detail, so its shape is a cross-effort
coordination point: widen it deliberately, not incidentally.

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
    strip_markers,
    unwrap_optional,
)
from agentworks.schema.markers import RefMarker

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from agentworks.schema._shape import FieldShape

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
    """The resolved annotation, reference markers stripped."""

    required: bool
    default: object
    """The declared default, or :data:`UNSET` when there is none. A
    ``default_factory`` is called to produce it, unless the factory takes
    validated data, which a doc walker does not have."""

    default_template: str | None
    """The owner-templated default name, unrendered: there is no owner at
    this layer. Scalar fields only, since a list has no single default
    identity."""

    description: str | None
    choices: tuple[object, ...]
    """``Literal`` values or ``Enum`` members in declaration order, empty
    when the field is open. Enum members, not their values: a presenter
    that wants the wire form reads ``.value``."""

    constraints: Mapping[str, object]
    """Normalized to plain keys and plain values (``{"min_length": 1}``)."""

    ref: RefMarker | None
    """The field's reference semantics, verbatim. For a marked list this
    is the ELEMENT's marker: it is what each element names."""

    nested_model: type[BaseModel] | None
    """Set when this field opens a nested block, whose fields follow this
    one in the stream at a longer path."""

    item_model: type[BaseModel] | None
    """Set when this field holds MANY blocks (a list of tables, a table
    of tables). The element's fields follow this one in the stream, once,
    under a :data:`SEQUENCE_ELEMENT` or :data:`MAPPING_KEY` path
    segment."""

    union_arms: tuple[UnionArm, ...]
    """The alternatives, when this field is a discriminated union. Not
    expanded inline: the presenter decides whether to render one arm, all
    of them, or a table, by recursing with ``iter_field_docs``."""


def iter_field_docs(model_cls: type[BaseModel]) -> Iterator[FieldDoc]:
    """``model_cls``'s fields in declaration order, nested blocks expanded
    inline depth-first, union arms left as handles.

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
    annotation = strip_markers(annotation)
    if annotation in _SCALAR_RENDERINGS:
        return _SCALAR_RENDERINGS[annotation]
    if get_origin(annotation) is Literal:
        return "one of: " + ", ".join(str(value) for value in get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "one of: " + ", ".join(str(member.value) for member in annotation)
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
    """
    if shape.nested_model is not None:
        return shape.nested_model, ()
    if shape.item_model is not None:
        segment = MAPPING_KEY if shape.collection is Collection.MAPPING else SEQUENCE_ELEMENT
        return shape.item_model, (segment,)
    return None, ()


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
        ref=marker,
        nested_model=shape.nested_model,
        item_model=shape.item_model,
        union_arms=tuple(UnionArm(tag=arm.tag, doc=model_doc(arm.model)) for arm in shape.arms),
    )


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
    annotation, _optional = unwrap_optional(annotation)
    if get_origin(annotation) is Literal:
        return get_args(annotation)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return tuple(annotation)
    return ()


def _constraints_of(field: FieldInfo) -> Mapping[str, object]:
    """The field's constraints, from wherever the author spelled them.

    For a collection field these are the ELEMENT's constraints, matching
    how ``ref`` reports the element's marker: both describe what one
    value has to look like.
    """
    constraints: dict[str, object] = {}
    for item in _constraint_carriers(field):
        if isinstance(item, RefMarker):
            continue
        for key in _CONSTRAINT_KEYS:
            value = getattr(item, key, None)
            if value is not None:
                constraints[key] = value
    return MappingProxyType(constraints)


def _constraint_carriers(field: FieldInfo) -> Iterator[object]:
    """Every metadata object that could carry a constraint.

    A ``Field(...)`` written inside an ``Annotated`` (rather than as the
    assigned default) survives as a nested ``FieldInfo`` holding its own
    metadata list, so that one level is flattened here.
    """
    for item in (*spine_metadata(field), *element_metadata(field)):
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


def _name_of(annotation: object) -> str:
    name = getattr(annotation, "__name__", None)
    return name if isinstance(name, str) else str(annotation)
