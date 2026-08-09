"""What a declared field IS, read off its annotation.

Both walkers in this package (reference extraction and the
field-reference stream) ask the same questions of a field: is it marked
as a reference, is it a list of them, does it open a nested block, is it
a discriminated union? Asking them once, here, is what keeps the two
walkers from drifting apart on a shape.

Internal to the package: the public surface is
``agentworks/schema/__init__``.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Final, TypeGuard, Union, get_args, get_origin

from pydantic import BaseModel, Discriminator, RootModel
from pydantic.errors import PydanticSchemaGenerationError, PydanticUndefinedAnnotation
from pydantic.fields import FieldInfo
from pydantic.json_schema import SkipJsonSchema

from agentworks.schema.markers import RefMarker
from agentworks.schema.shorthand import (
    UnionScalarShorthand,
    scalar_shorthand_of,
    union_scalar_resolution,
)
from agentworks.schema.structural import StructuralUnion

if TYPE_CHECKING:
    from collections.abc import Callable

#: The runtime class behind ``SkipJsonSchema[X]``, which expands to
#: ``Annotated[X, SkipJsonSchema()]``, so the metadata item a field
#: carries is an INSTANCE of it. Bound to a plainly-typed name because
#: type checkers read the imported symbol as a parameterized alias and
#: refuse it as an ``isinstance`` target.
_SKIP_MARKER: Final[type] = SkipJsonSchema


@dataclass(frozen=True)
class UnionArmType:
    """One arm of a discriminated union: the tag that selects it and the
    model it selects."""

    tag: str
    model: type[BaseModel]


class Collection(Enum):
    """How a field holding MANY values addresses them."""

    SEQUENCE = "sequence"
    """A list, tuple, or set: elements are addressed by position."""

    MAPPING = "mapping"
    """A table: values are addressed by an operator-chosen key."""


@dataclass(frozen=True, kw_only=True)
class FieldShape:
    """The classified shape of one declared field.

    A field is exactly one of: a marked scalar (``marker``), a collection
    of marked scalars or of models (``collection``, with ``item_marker``
    or ``item_model``), a nested block (``nested_model``), a
    tagged or structural model union (``arms`` / ``structural_arms``), or
    an ordinary scalar, which both walkers treat as carrying no references.
    """

    annotation: object
    """What an operator may WRITE here, as an annotation: reference
    markers stripped out at every depth, every model declaring a scalar
    shorthand widened to the union it accepts (``EnvEntry`` reads
    ``str | EnvEntry``), and an owner-templated reference widened to
    accept ``null``. Optionality is preserved: ``str | None`` stays
    ``str | None``. See :func:`accepted_annotation`."""

    optional: bool
    """Whether ``None`` is an accepted value. True for an owner-templated
    reference, which reads an explicit ``null`` as the omission it
    resolves; :attr:`annotation` says the same thing."""

    marker: RefMarker | None
    """The marker on the field itself: the field names one Resource."""

    collection: Collection | None
    """Set when the field holds many values, saying how they are
    addressed. What each value IS is ``item_marker`` or ``item_model``."""

    item_marker: RefMarker | None
    """The marker on a collection's elements: the field names many
    Resources."""

    item_model: type[BaseModel] | None
    """The model each element of a collection holds."""

    nested_model: type[BaseModel] | None
    """The model this field opens a nested block of."""

    discriminator: str | None
    """The field name carrying a discriminated union's tag."""

    arms: tuple[UnionArmType, ...]
    """The union's arms, in declaration order; empty unless the field is
    a discriminated union of models."""

    union_scalar_shorthand: UnionScalarShorthand | None
    """The explicit scalar-to-arm dispatch for this union, if declared."""

    item_discriminator: str | None
    """:attr:`discriminator` one level down: the tag field a COLLECTION's
    elements are dispatched on (``list[Annotated[A | B,
    Discriminator("kind")]]``).

    Kept apart from :attr:`discriminator` for the reason ``item_marker``
    is kept apart from ``marker``: one describes the field, the other
    describes one value inside it. A field holding a collection has no
    arms of its OWN, so reusing the field-level attributes would tell a
    walker to dispatch the collection itself on a tag."""

    item_arms: tuple[UnionArmType, ...]
    """:attr:`arms` one level down: the arms one ELEMENT of a collection
    may be, empty unless the elements are a discriminated union of models.

    A collection of tagged blocks is not a shape the framework ships
    today, and it is one any capability or plugin author can write. (It
    was once true that every discriminated union here was a top-level
    capability config. The auth and placement unions on azure, aws, and
    lima are nested unions whose arms are plain models, so what is left
    unshipped is the COLLECTION of them, not the nesting.)
    Left unclassified, its elements read as an undiscriminated union,
    which no walker expands: a secret named inside such an element would
    be absent from the dependency graph with nothing reported."""

    item_union_scalar_shorthand: UnionScalarShorthand | None
    """:attr:`union_scalar_shorthand` for one collection element."""

    union_members: tuple[object, ...]
    """The members of an UNDISCRIMINATED union, in declaration order, as
    raw annotations. Empty unless the field is a union with no
    discriminator (``str | AccountRef``, a secret backend's mapping).

    The error bridge reads this because pydantic reports one error per
    member and prefixes each with the member's own name, a segment the
    operator never wrote. Reference extraction reads it to ask whether a
    raw table could be a member OTHER than :attr:`union_model`, which is
    what decides whether a table addresses the block or nothing."""

    union_model: type[BaseModel] | None
    """The ONE model among :attr:`union_members`, when exactly one of them
    is a model (``str | AccountRef``): the single block an operator could
    write where this field goes.

    Kept apart from :attr:`nested_model` rather than folded into it,
    because a field that MAY be a block is not a field that IS one, and
    both other readers of that attribute depend on the difference. The
    error bridge would stop reading :attr:`union_members` and start
    rendering pydantic's per-member name segments (``AccountRef``) as path
    segments the operator never wrote. Reference extraction would walk a
    raw string as though it were a block, which for a ROOT-model arm is a
    wholly invented edge; it reads this attribute through a selector of
    its own, which walks the arm only where a table can BE the arm.

    The field-documentation stream reads it to expand what the block
    contains: without it a shape whose emitted schema spells out both arms
    documents the model arm as the bare word "table"."""

    item_union_members: tuple[object, ...]
    """:attr:`union_members` one level down: the members of an
    undiscriminated union held by a COLLECTION's elements
    (``dict[str, str | dict[str, object] | Literal[False]]``, a secret's
    ``backend_mappings``).

    Kept apart from :attr:`union_members` for the same reason
    ``item_marker`` is kept apart from ``marker``: one describes the
    field, the other describes one value inside it. Only the error bridge
    reads it, and for the same reason it reads ``union_members``: without
    it the walk loses track at the element and renders pydantic's member
    labels (``backend_mappings.b.str``) as path segments."""

    item_union_model: type[BaseModel] | None
    """:attr:`union_model` one level down: the one block an ELEMENT of a
    collection could be (``dict[str, str | AccountRef]``).

    No shipped field has this shape (the shipped scalar-or-block union is
    a secret backend's whole mapping, and ``backend_mappings``'s elements
    offer a bare table rather than a model), and it is one any capability
    or plugin author can write. Classified for the reason
    :attr:`item_arms` is: the field-documentation surfaces would otherwise
    render such an element as an opaque "table" while the emitted schema
    spelled out its properties, which is the same disagreement between the
    two derivations, one level down, and a reference marked inside such an
    element would be missing from the dependency graph."""

    structural_arms: tuple[type[BaseModel], ...]
    """The closed model arms of an explicitly structural untagged union.

    Unlike :attr:`arms`, no operator-written tag selects one. A raw table
    addresses an arm only when :func:`structural_arm_for` finds exactly one
    arm whose required and allowed keys match it.
    """

    item_structural_arms: tuple[type[BaseModel], ...]
    """:attr:`structural_arms` one collection-element level down."""

    structural_null_companions: bool
    """Whether this structural union accepts foreign-arm ``null`` keys."""

    item_structural_null_companions: bool
    """:attr:`structural_null_companions` one collection-element level down."""

    @property
    def block(self) -> type[BaseModel] | None:
        """The model whose fields the field-documentation stream expands
        under this field: the block it IS, or the one block a union
        offers.

        Only that stream, which is why this is a derived pairing rather
        than one classified attribute: the stream documents every block an
        operator MAY write, while the other two readers must decide which
        one an operator DID write, and each reads the attributes that
        answer its own question (see :attr:`union_model`). Paired here
        rather than at the two places the stream needs it, so what the
        stream SAYS a field contains and what it actually streams under it
        cannot come apart.
        """
        return self.nested_model or self.union_model

    @property
    def item_block(self) -> type[BaseModel] | None:
        """:attr:`block` for ONE element of a collection."""
        return self.item_model or self.item_union_model


def spine_metadata(field: FieldInfo) -> list[object]:
    """Every ``Annotated`` metadata item attached to the field's OWN
    value, wherever the author spelled it.

    There are three places, and a lookup that misses one is a silent
    wrong answer rather than an error, because pydantic itself accepts
    all three:

    - ``field.metadata``, where pydantic lifts metadata written outside
      a union (``Annotated[str | None, SecretRef(...)]``);
    - the annotation itself, when a union sits inside the ``Annotated``
      (``Annotated[Lima | Proxmox | None, Discriminator("name")]``);
    - a union ARM, when the ``Annotated`` sits inside the union
      (``Annotated[Lima | Proxmox, Discriminator("name")] | None``,
      ``Annotated[str, SecretRef(...)] | int``).

    Metadata on a COLLECTION's elements is not here; that is
    :func:`element_metadata`, and the two are kept apart because a
    marker on the field means one Resource while a marker on its
    elements means many.
    """
    items = list(field.metadata)
    base, metadata = split_annotated(field.annotation)
    items.extend(metadata)
    if _is_union(base):
        for arg in get_args(base):
            _arm, arm_metadata = split_annotated(arg)
            items.extend(arm_metadata)
    return items


def element_metadata(field: FieldInfo) -> list[object]:
    """Every ``Annotated`` metadata item on the elements of a collection
    field, or nothing when the field holds a single value."""
    inner, _optional, _metadata = _unwrapped_annotation(field.annotation)
    found = _collection_element(inner)
    if found is None:
        return []
    _kind, element = found
    _element, _element_optional, metadata = _unwrapped_annotation(element)
    return metadata


def marker_of(field: FieldInfo) -> RefMarker | None:
    """The reference marker on the field ITSELF, if any. Cheaper than a
    whole :func:`shape_of` for the callers that only want the marker."""
    return _first_marker(spine_metadata(field))


def shape_of(field: FieldInfo) -> FieldShape:
    """Classify ``field``. Reads annotations only; runs no user code."""
    inner, optional, inner_metadata = _unwrapped_annotation(field.annotation)
    marker = marker_of(field)

    collection: Collection | None = None
    item_marker: RefMarker | None = None
    item_model: type[BaseModel] | None = None
    nested_model: type[BaseModel] | None = None
    discriminator: str | None = None
    arms: tuple[UnionArmType, ...] = ()
    union_scalar_shorthand: UnionScalarShorthand | None = None
    item_discriminator: str | None = None
    item_arms: tuple[UnionArmType, ...] = ()
    item_union_scalar_shorthand: UnionScalarShorthand | None = None
    union_members: tuple[object, ...] = ()
    union_model: type[BaseModel] | None = None
    item_union_members: tuple[object, ...] = ()
    item_union_model: type[BaseModel] | None = None
    structural_arms: tuple[type[BaseModel], ...] = ()
    item_structural_arms: tuple[type[BaseModel], ...] = ()
    structural_null_companions = False
    item_structural_null_companions = False

    found = _collection_element(inner)
    if found is not None:
        collection, element = found
        element, _element_optional, element_meta = _unwrapped_annotation(element)
        item_marker = _first_marker(element_meta)
        item_discriminator = _element_discriminator(element_meta) if _is_model(element) or _is_union(element) else None
        if item_discriminator is not None:
            # Same order as the field-level branch below, and for the same
            # reason: a tagged union addresses one arm from a raw blob and
            # an untagged one addresses none, so asking about the tag first
            # is what keeps both a many-arm union and pydantic's collapsed
            # one-arm model from reading as an ordinary nested block.
            item_arms = _arms_of(element, item_discriminator)
            item_union_scalar_shorthand = _sole_union_scalar_shorthand(element_meta)
        elif _is_model(element):
            item_model = element
        elif _is_union(element):
            if (structural := _structural_union_of(element_meta)) is not None:
                item_union_members = tuple(split_annotated(arg)[0] for arg in get_args(element))
                item_structural_arms = _closed_model_arms(item_union_members)
                item_structural_null_companions = structural.canonicalize_null_companions
            else:
                item_union_members = tuple(split_annotated(arg)[0] for arg in get_args(element))
                item_union_model = _sole_model(item_union_members)
    elif _is_model(inner) or _is_union(inner):
        discriminator = _discriminator_of(field)
        if discriminator is not None:
            # A discriminated union of ONE arm is not a union at all:
            # ``Union[(X,)]`` is ``X``. Pydantic still dispatches on the
            # tag (it answers ``union_tag_invalid`` for a wrong one), so
            # reading the collapsed form as "a nested block" would lose
            # the tag: a failure's loc still leads with it, and a walker
            # that does not know it is a tag renders it as a field the
            # operator never wrote. Live the moment a capability kind has
            # a single registered implementation.
            arms = _arms_of(inner, discriminator)
            union_scalar_shorthand = _sole_union_scalar_shorthand(spine_metadata(field))
        elif _is_model(inner):
            nested_model = inner
        elif (structural := _structural_union_of([*field.metadata, *inner_metadata])) is not None:
            union_members = tuple(split_annotated(arg)[0] for arg in get_args(inner))
            structural_arms = _closed_model_arms(union_members)
            structural_null_companions = structural.canonicalize_null_companions
        else:
            union_members = tuple(split_annotated(arg)[0] for arg in get_args(inner))
            union_model = _sole_model(union_members)

    return FieldShape(
        annotation=accepted_annotation(
            field.annotation,
            marker,
            discriminator=discriminator,
            union_scalar_shorthand=union_scalar_shorthand,
        ),
        # An owner-templated field accepts ``None`` whatever its
        # annotation says, so the flag and the annotation are widened by
        # the one answer rather than each deciding for itself.
        optional=optional or (marker is not None and marker.default_template is not None),
        marker=marker,
        collection=collection,
        item_marker=item_marker,
        item_model=item_model,
        nested_model=nested_model,
        discriminator=discriminator,
        arms=arms,
        union_scalar_shorthand=union_scalar_shorthand,
        item_discriminator=item_discriminator,
        item_arms=item_arms,
        item_union_scalar_shorthand=item_union_scalar_shorthand,
        union_members=union_members,
        union_model=union_model,
        item_union_members=item_union_members,
        item_union_model=item_union_model,
        structural_arms=structural_arms,
        item_structural_arms=item_structural_arms,
        structural_null_companions=structural_null_companions,
        item_structural_null_companions=item_structural_null_companions,
    )


@dataclass(frozen=True)
class _ClosedShape:
    """The key language one closed mapping-shaped model accepts."""

    required: frozenset[str]
    allowed: frozenset[str]


def structural_arm_and_value(
    arms: tuple[type[BaseModel], ...],
    value: object,
    *,
    canonicalize_null_companions: bool = False,
) -> tuple[type[BaseModel] | None, object]:
    """The structural arm ``value`` addresses and its canonical spelling.

    Old combined models could persist a selected field beside the other
    arm's explicit ``null`` field. An explicit structural union treats that
    null as a companion, not a second arm, only when removing it leaves one
    uniquely addressed arm. Unknown keys and non-null cross-arm values stay
    untouched so validation can refuse them.
    """
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        return None, value
    keys = frozenset(value)
    shapes = tuple((arm, shape) for arm in arms if (shape := _closed_shape_of(arm)) is not None)
    matched = [arm for arm, shape in shapes if _matches(shape, keys)]
    if len(matched) == 1:
        return matched[0], value
    if not canonicalize_null_companions:
        return None, value
    if not isinstance(value, dict):
        return None, value
    known = frozenset(name for _arm, shape in shapes for name in shape.allowed)
    if not keys <= known:
        return None, value
    candidates: list[tuple[type[BaseModel], dict[object, object]]] = []
    for arm, shape in shapes:
        canonical = {
            key: item
            for key, item in value.items()
            if item is not None or not isinstance(key, str) or key in shape.allowed
        }
        if len(canonical) != len(value) and _matches(shape, frozenset(canonical)):
            candidates.append((arm, canonical))
    return candidates[0] if len(candidates) == 1 else (None, value)


def structural_arm_for(arms: tuple[type[BaseModel], ...], value: object) -> type[BaseModel] | None:
    """The one structural arm ``value`` addresses, or ``None``.

    Values do not participate. A wrong scalar inside an otherwise unique
    arm still addresses that arm, then extraction omits the malformed edge
    and validation reports the scalar error. Unknown keys, missing required
    keys, and a key set accepted by several arms address nothing.
    """
    arm, _canonical = structural_arm_and_value(arms, value)
    return arm


def structurally_addressable_arms(arms: tuple[type[BaseModel], ...]) -> tuple[type[BaseModel], ...]:
    """Every arm for which at least one unique accepted key set exists.

    Registration conformance uses this as the static counterpart of
    :func:`structural_arm_for`: a marker may live only in an arm the raw
    walker can select for some document validation accepts.
    """
    return tuple(arm for arm in arms if _arm_is_addressable(arm, arms))


def structural_union_error(model_cls: type[BaseModel]) -> str | None:
    """Why a structural union under ``model_cls`` cannot mean ``oneOf``.

    Pydantic validates an untagged union by trying its members, while the
    emitted contract says exactly one arm may match. Registration calls
    this guard so an open, malformed, or overlapping declaration is loud
    even when every arm is marker-free.
    """
    return _structural_union_error(model_cls, ())


def _structural_union_error(
    model_cls: type[BaseModel],
    visiting: tuple[type[BaseModel], ...],
) -> str | None:
    if model_cls in visiting:
        return None
    fields = model_fields_of(model_cls)
    if fields is None:
        return None
    visiting = (*visiting, model_cls)
    for name, field in fields.items():
        own, elements = _structural_declarations(field)
        declarations = (
            (f"{model_cls.__name__}.{name}", own),
            (f"{model_cls.__name__}.{name} elements", elements),
        )
        for location, declaration in declarations:
            if declaration is None:
                continue
            members, discriminator = declaration
            if discriminator is not None:
                return (
                    f"{location} combines StructuralUnion with discriminator {discriminator!r}; "
                    "a structural union is selector-free and must be addressed only by its "
                    "arms' required and allowed keys"
                )
            reason = _structural_members_error(location, members)
            if reason is not None:
                return reason
        for nested in models_in(field.annotation):
            reason = _structural_union_error(nested, visiting)
            if reason is not None:
                return reason
    return None


_StructuralDeclaration = tuple[tuple[object, ...], object | None]


def _structural_declarations(
    field: FieldInfo,
) -> tuple[_StructuralDeclaration | None, _StructuralDeclaration | None]:
    """Structural-union member declarations on a field and its elements."""
    inner, _optional, own_metadata = _unwrapped_annotation(field.annotation)
    own = (
        (
            tuple(split_annotated(arg)[0] for arg in get_args(inner)),
            _field_discriminator_declaration(field),
        )
        if _has_structural_union([*field.metadata, *own_metadata])
        else None
    )
    found = _collection_element(inner)
    if found is None:
        return own, None
    _collection, element = found
    element, _element_optional, element_metadata = _unwrapped_annotation(element)
    elements = (
        (
            tuple(split_annotated(arg)[0] for arg in get_args(element)),
            _metadata_discriminator_declaration(element_metadata),
        )
        if _has_structural_union(element_metadata)
        else None
    )
    return own, elements


def _field_discriminator_declaration(field: FieldInfo) -> object | None:
    """Any selector declared on a field, including callable ones."""
    if field.discriminator is not None:
        if isinstance(field.discriminator, Discriminator):
            return field.discriminator.discriminator
        return field.discriminator
    return _metadata_discriminator_declaration(spine_metadata(field))


def _metadata_discriminator_declaration(metadata: list[object]) -> object | None:
    """Any discriminator selector written in one metadata sequence."""
    for item in metadata:
        if isinstance(item, UnionScalarShorthand):
            return item.discriminator
        if isinstance(item, Discriminator):
            return item.discriminator
        if isinstance(item, FieldInfo) and item.discriminator is not None:
            return item.discriminator
    return None


def _structural_members_error(location: str, members: tuple[object, ...]) -> str | None:
    if len(members) < 2 or any(not _is_model(member) for member in members):
        return f"{location} marks a structural union, but its members are not two or more model arms"
    arms = tuple(member for member in members if _is_model(member))
    for arm in arms:
        if _closed_shape_of(arm) is None:
            return f"{location} has open arm {arm.__name__}; every structural arm must be a closed mapping model"
        fields = model_fields_of(arm)
        if fields is None:
            continue
        for name, field in fields.items():
            if field.validation_alias is not None:
                return (
                    f"{location} arm {arm.__name__}.{name} declares validation alias "
                    f"{field.validation_alias!r}; structural arms must use their field names "
                    "because shape selection reads the operator's raw keys"
                )
    for index, left in enumerate(arms):
        for right in arms[index + 1 :]:
            if _table_shapes_overlap(left, right):
                return (
                    f"{location} has overlapping arms {left.__name__} and {right.__name__}; "
                    "their required and allowed keys must make every table match at most one"
                )
            if _scalar_shorthands_overlap(left, right):
                return (
                    f"{location} has overlapping scalar shorthands on {left.__name__} and "
                    f"{right.__name__}; one scalar must match at most one arm"
                )
    return None


def _table_shapes_overlap(left: type[BaseModel], right: type[BaseModel]) -> bool:
    left_shape = _closed_shape_of(left)
    right_shape = _closed_shape_of(right)
    if left_shape is None or right_shape is None:
        return False
    return left_shape.required | right_shape.required <= left_shape.allowed & right_shape.allowed


def _scalar_shorthands_overlap(left: type[BaseModel], right: type[BaseModel]) -> bool:
    left_shorthand = scalar_shorthand_of(left)
    right_shorthand = scalar_shorthand_of(right)
    if left_shorthand is None or right_shorthand is None:
        return False
    annotations = {left_shorthand.annotation, right_shorthand.annotation}
    return len(annotations) == 1 or annotations == {int, float}


def _field_has_structural_union(field: FieldInfo) -> bool:
    """Whether metadata on the field's own union declares the shape."""
    _base, _optional, annotation_metadata = _unwrapped_annotation(field.annotation)
    return _has_structural_union([*field.metadata, *annotation_metadata])


def _has_structural_union(metadata: list[object]) -> bool:
    return _structural_union_of(metadata) is not None


def _structural_union_of(metadata: list[object]) -> StructuralUnion | None:
    """The structural-union declaration in one metadata sequence, if any."""
    return next((item for item in metadata if isinstance(item, StructuralUnion)), None)


def _closed_model_arms(members: tuple[object, ...]) -> tuple[type[BaseModel], ...]:
    """All members as closed models, or no arms for a malformed declaration."""
    models = tuple(member for member in members if _is_model(member))
    if len(models) != len(members) or len(models) < 2:
        return ()
    return models if all(_closed_shape_of(model) is not None for model in models) else ()


def _closed_shape_of(model: type[BaseModel]) -> _ClosedShape | None:
    """The required and allowed field names of one closed model arm."""
    if issubclass(model, RootModel) or model.model_config.get("extra") != "forbid":
        return None
    fields = model_fields_of(model)
    if fields is None:
        return None
    required = frozenset(
        name
        for name, field in fields.items()
        if field.is_required()
        and not ((marker := marker_of(field)) is not None and marker.default_template is not None)
    )
    return _ClosedShape(required=required, allowed=frozenset(fields))


def _matches(shape: _ClosedShape, keys: frozenset[str]) -> bool:
    return shape.required <= keys <= shape.allowed


def _arm_is_addressable(arm: type[BaseModel], arms: tuple[type[BaseModel], ...]) -> bool:
    """Whether the arm's accepted key language has a unique member."""
    # Registration rejects overlaps first, but ``reference_marker_error``
    # is also a standalone public guard. The regression
    # ``test_an_unaddressable_marker_is_still_refused`` keeps this load-bearing.
    shape = _closed_shape_of(arm)
    if shape is None:
        return False
    competitors = tuple(other for other in arms if other is not arm and _closed_shape_of(other) is not None)
    clauses: list[tuple[tuple[str, bool], ...]] = []
    for other in competitors:
        other_shape = _closed_shape_of(other)
        if other_shape is None:
            continue
        if other_shape.required - shape.allowed or shape.required - other_shape.allowed:
            continue
        choices = tuple(
            [(key, False) for key in other_shape.required - shape.required]
            + [(key, True) for key in shape.allowed - other_shape.allowed]
        )
        if not choices:
            return False
        clauses.append(choices)
    return _clauses_are_satisfiable(clauses, set(shape.required), set())


def _clauses_are_satisfiable(
    clauses: list[tuple[tuple[str, bool], ...]],
    included: set[str],
    excluded: set[str],
) -> bool:
    """Small monotone SAT solver for structural-arm addressability."""
    remaining: list[tuple[tuple[str, bool], ...]] = []
    for clause in clauses:
        if any((key in included) if present else (key in excluded) for key, present in clause):
            continue
        available = tuple(
            (key, present) for key, present in clause if (key not in excluded if present else key not in included)
        )
        if not available:
            return False
        remaining.append(available)
    if not remaining:
        return True
    choice = min(remaining, key=len)
    for key, present in choice:
        next_included = included | {key} if present else included
        next_excluded = excluded if present else excluded | {key}
        if _clauses_are_satisfiable(remaining, next_included, next_excluded):
            return True
    return False


def is_hidden(field: FieldInfo) -> bool:
    """Whether ``field`` is framework surface rather than operator surface.

    ``SkipJsonSchema`` is the one marker that says so, and it says it
    once for BOTH derivations: pydantic drops the field from
    ``model_json_schema`` natively, and the field-reference stream drops
    it here. That is what lets a declared-resource row carry its
    ``origin`` and ``declared_at`` beside the operator's spec fields
    without either surface having to keep an exclusion list.

    Read off :func:`spine_metadata` rather than ``field.metadata``, so
    the answer does not depend on which of the three legal places the
    author spelled the annotation in.
    """
    return any(isinstance(item, _SKIP_MARKER) for item in spine_metadata(field))


def model_is_complete(model_cls: type[BaseModel]) -> bool:
    """Whether ``model_cls``'s fields resolve, so something can validate,
    extract, or render against it.

    The public form of :func:`model_fields_of`'s ``None`` answer, for the
    one caller outside this package that needs it: registration
    conformance refuses a config model with an unresolved annotation,
    which is what keeps the extractor's "an incomplete model contributes
    nothing" branch unreachable in practice.
    """
    return model_fields_of(model_cls) is not None


def union_scalar_shorthand_error(model_cls: type[BaseModel]) -> str | None:
    """Why a tagged-union scalar dispatch is inconsistent, or ``None``.

    An arm model may accept a scalar on its own, but tag dispatch happens
    before arm validation. Every discriminated union exposing such an arm
    must therefore opt into scalar dispatch explicitly. This recursive
    check is registration conformance's guarantee that validation and the
    two raw walkers never infer different choices.
    """
    return _union_scalar_error(model_cls, ())


def _union_scalar_error(
    model_cls: type[BaseModel],
    visiting: tuple[type[BaseModel], ...],
) -> str | None:
    if model_cls in visiting:
        return None
    fields = model_fields_of(model_cls)
    if fields is None:
        return None
    visiting = (*visiting, model_cls)
    for name, field in fields.items():
        shape = shape_of(field)
        reason = _one_union_scalar_error(
            f"{model_cls.__name__}.{name}",
            field.annotation,
            shape.discriminator,
            shape.arms,
            spine_metadata(field),
        )
        if reason is not None:
            return reason
        element = element_annotation(field.annotation)
        if element is not None:
            reason = _one_union_scalar_error(
                f"{model_cls.__name__}.{name}'s element",
                element,
                shape.item_discriminator,
                shape.item_arms,
                element_metadata(field),
            )
            if reason is not None:
                return reason
        for child in models_in(field.annotation):
            reason = _union_scalar_error(child, visiting)
            if reason is not None:
                return reason
    return None


def _one_union_scalar_error(
    field_name: str,
    annotation: object,
    discriminator: str | None,
    arms: tuple[UnionArmType, ...],
    metadata: list[object],
) -> str | None:
    declarations = [item for item in metadata if isinstance(item, UnionScalarShorthand)]
    if len(declarations) > 1:
        return f"{field_name} declares {len(declarations)} union scalar shorthands; exactly one can select a scalar arm"
    arm_shorthands = [arm.model.__name__ for arm in arms if scalar_shorthand_of(arm.model) is not None]
    if not declarations:
        if discriminator is not None and arm_shorthands:
            return (
                f"{field_name} has scalar shorthand on tagged arm(s) {', '.join(arm_shorthands)} "
                "but declares no UnionScalarShorthand to select one before tag dispatch"
            )
        return None
    declaration = declarations[0]
    if discriminator is None:
        return f"{field_name} declares UnionScalarShorthand but is not a discriminated union"
    if declaration.discriminator != discriminator:
        return (
            f"{field_name} dispatches tables by {discriminator!r} but its UnionScalarShorthand "
            f"declares {declaration.discriminator!r}"
        )
    reason = union_scalar_resolution(annotation, declaration)
    if isinstance(reason, str):
        return f"{field_name} has an invalid UnionScalarShorthand: {reason}"
    _shorthand, tag = reason
    if not any(arm.model is declaration.arm and arm.tag == tag for arm in arms):
        return (
            f"{field_name} selects {declaration.arm.__name__} as scalar shorthand, but tag {tag!r} "
            "does not address that arm"
        )
    return None


def is_model(annotation: object) -> TypeGuard[type[BaseModel]]:
    """Whether ``annotation`` is a model class. Shared so the bridge asks
    the same question this module does."""
    return _is_model(annotation)


def accepts_table(annotation: object) -> bool:
    """Whether a raw TABLE could satisfy ``annotation``.

    :func:`table_addresses_block` asks this of an undiscriminated union's
    members, because being a table is the only thing that selects such a
    union's block from a raw blob: no tag names it. The answer is a fact
    only while the block is the ONE member a table could be, so it is
    asked of the others.

    Recognized by spelling, not by trying a value against it: a model, any
    mapping type (``dict``, ``Mapping``, a ``TypedDict``, which is a
    ``dict`` subclass at runtime), and ``object``/``Any``, which accept
    anything. A member outside that list reads as scalar-shaped, and a
    custom type with a before-validator that turns a table into a scalar
    would fool it. That is a boundary rather than an oversight: this
    module reads annotations and never runs user code, so a validator is
    exactly what it cannot see.
    """
    if _is_model(annotation) or annotation is object or annotation is typing.Any:
        return True
    origin = get_origin(annotation) or annotation
    return isinstance(origin, type) and issubclass(origin, Mapping)


def table_addresses_block(model: type[BaseModel], members: tuple[object, ...]) -> bool:
    """Whether a raw table names ``model`` among an undiscriminated
    union's ``members`` before validation.

    Nothing tags such a union, so the value's own shape is the only
    address a raw blob offers: a table IS the block, but only while the
    block is the sole member a table could satisfy
    (:func:`accepts_table`). A union offering a bare table beside the
    model (``dict[str, str] | Creds``) addresses no arm pre-validation,
    since pydantic settles that one by trying the arms and preferring
    whichever fits, and naming the block would invent an edge for a value
    that validates as the table.

    One function because two callers must agree on it or the exact defect
    it decides comes back between them: reference extraction walks the
    block only when this is true, and registration conformance counts the
    block walker-reachable only when extraction would walk it, so a
    marker inside a block extraction refuses to walk is refused at
    registration instead of silently never extracted.
    """
    return not any(member is not model and accepts_table(member) for member in members)


def addressed_arm_model(
    arms: tuple[UnionArmType, ...],
    discriminator: str,
    shorthand: UnionScalarShorthand | None,
    value: object,
) -> type[BaseModel] | None:
    """The discriminated-union arm a raw value addresses, or ``None``.

    A table addresses the arm named by its tag. A bare scalar addresses an
    arm only when the UNION explicitly declares that dispatch. An arm's
    own :class:`~agentworks.schema.ScalarShorthand` is not enough because
    pydantic must choose a tag before it validates an arm.

    Kept here, beside the other raw-shape addressing rule, because the two
    walkers must agree on which arm a document selected. A disagreement
    would validate a secret reference that the dependency graph omitted.
    """
    if isinstance(value, Mapping):
        tag = value.get(discriminator)
        return next((arm.model for arm in arms if arm.tag == tag), None)
    if shorthand is None:
        return None
    matches = [arm.model for arm in arms if arm.model is shorthand.arm]
    scalar = scalar_shorthand_of(shorthand.arm)
    if len(matches) != 1 or scalar is None:
        return None
    return shorthand.arm if type(value) is scalar.annotation else None


def model_fields_of(model_cls: type[BaseModel]) -> dict[str, FieldInfo] | None:
    """``model_cls``'s fields, or ``None`` when the model cannot be built.

    A model with an unresolved forward reference has no usable fields.
    One rebuild attempt is made (the annotation may have become
    resolvable since class definition); a model still incomplete after
    it is reported as unusable rather than raising, and each caller
    decides what that means for it. Reference extraction contributes
    nothing for such a model, while the field-reference stream raises,
    which is the difference between a walker whose caller cannot handle
    an exception and one whose caller is a renderer.
    """
    if not model_cls.__pydantic_complete__:
        _attempt_rebuild(model_cls)
        if not model_cls.__pydantic_complete__:
            return None
    return dict(model_cls.model_fields)


def _attempt_rebuild(model_cls: type[BaseModel]) -> None:
    """Try once to resolve a model's annotations, without raising.

    ``raise_errors=False`` covers only the undefined-annotation case. A
    forward reference that RESOLVES, to a type pydantic cannot build a
    schema for, raises ``PydanticSchemaGenerationError`` straight out of
    the rebuild, and a caller reaching such a model through the
    annotation graph could not have screened for it.

    This is not the blanket suppression the extractor's contract forbids,
    and the distinction is worth stating: a blanket guard would swallow
    bugs in the WALK, which is what turns a wrong graph silent. This
    names the two documented failures of the single pydantic call this
    module makes, and the model is reported as unusable either way, which
    is the same answer its caller already handles.
    """
    with suppress(PydanticSchemaGenerationError, PydanticUndefinedAnnotation):
        model_cls.model_rebuild(raise_errors=False)


def _collection_element(annotation: object) -> tuple[Collection, object] | None:
    """What kind of collection ``annotation`` is and what ONE element of
    it holds, or ``None`` when the field holds a single value.

    Recognized by what the origin IS rather than by a list of concrete
    classes, the way :func:`accepts_table` already answers the neighbouring
    question. ``Sequence[X]`` and ``list[X]`` are the same shape to every
    reader of a :class:`FieldShape`, and the raw values a YAML or TOML
    frontend produces are lists and tables either way, so classifying only
    the concrete spelling would leave the abstract one holding a marker
    that nothing extracts. Silence there is a gating bypass rather than a
    cosmetic gap: the dependency graph is built from the extracted edges
    BEFORE anything validates, so a secret named under an unclassified
    collection is never gated, never resolved, and never reported.

    Mapping is asked FIRST because every mapping is also a ``Collection``
    and an ``Iterable``, and it addresses its VALUES: reading a table as a
    sequence would classify its element type off the wrong type argument.

    Two shapes are deliberately not collections here. A fixed-length
    heterogeneous tuple (``tuple[str, int]``) has no single element type,
    and it is not a shape an operator writes in YAML. A NESTED collection
    (``dict[str, list[X]]``) is one this flat classifier cannot describe:
    :class:`FieldShape` says what a field holds and what ONE value inside
    it holds, and there is no third level to put the inner element in.
    Both fall through to ``None``, and a marker underneath either is
    refused at registration by
    :func:`~agentworks.schema.reference_marker_error` rather than silently
    dropped, which is what keeps this function's list of recognized shapes
    from being a list of silent failures.
    """
    origin = get_origin(annotation)
    args = get_args(annotation)
    if not isinstance(origin, type):
        return None
    if issubclass(origin, Mapping):
        return (Collection.MAPPING, args[1]) if len(args) == 2 else None
    if origin is tuple:
        return (Collection.SEQUENCE, args[0]) if len(args) == 2 and args[1] is Ellipsis else None
    if issubclass(origin, (Sequence, AbstractSet)):
        return (Collection.SEQUENCE, args[0]) if len(args) == 1 else None
    return None


def _first_marker(metadata: list[object]) -> RefMarker | None:
    for item in metadata:
        if isinstance(item, RefMarker):
            return item
    return None


def markers_in(annotation: object) -> tuple[RefMarker, ...]:
    """Every reference marker written anywhere inside ``annotation``, at
    any depth, as the marker OBJECTS the author attached.

    The counterpart to the classifier rather than a part of it: where
    :func:`shape_of` reports the markers it can place (``marker`` for the
    field's own value, ``item_marker`` for one element of a collection),
    this reports the markers that are THERE. A marker in the second set
    and not the first is one no walker will ever read, and the whole
    point of returning the objects rather than a count is that the two
    sets are compared by identity: see
    :func:`~agentworks.schema.reference_marker_error`, which turns the
    difference into a registration refusal.

    The walk stops at a model, because a model's markers belong to its own
    fields and are judged when that model is judged. It is finite for the
    same reason: a recursive model is reached as a model, not as a tree of
    annotations.
    """
    base, metadata = split_annotated(annotation)
    found = [item for item in metadata if isinstance(item, RefMarker)]
    if _is_model(base):
        return tuple(found)
    for arg in get_args(base):
        found.extend(markers_in(arg))
    return tuple(found)


def models_in(annotation: object) -> tuple[type[BaseModel], ...]:
    """Every model class written anywhere inside ``annotation``, at any
    depth, deduplicated in first-appearance order.

    :func:`markers_in`'s counterpart one level up: where that reports the
    markers an annotation carries, this reports the models it offers,
    which is the set VALIDATION can construct a value of from the field.
    :func:`~agentworks.schema.reference_marker_error` subtracts the
    models a walker reaches from this answer, and what remains is the
    set a document can fill while no walker ever reads it.

    The walk stops AT a model rather than descending into its fields,
    for the reason :func:`markers_in` stops there: a model's own contents
    are judged when that model is judged. It is finite for the same
    reason: a recursive model is reached as a model, not as a tree of
    annotations.
    """
    base, _metadata = split_annotated(annotation)
    if _is_model(base):
        return (base,)
    found: dict[type[BaseModel], None] = {}
    for arg in get_args(base):
        found.update(dict.fromkeys(models_in(arg)))
    return tuple(found)


def strip_markers(annotation: object) -> object:
    """``annotation`` with every reference marker removed, at any depth.

    A marker says what a field MEANS, which the walkers report
    separately; what is left is the type an operator has to write.
    Annotations this cannot rebuild (a ``Literal``, a callable) are
    returned unchanged, since a marker cannot appear inside one anyway.
    """
    base, metadata = split_annotated(annotation)
    kept = [item for item in metadata if not isinstance(item, RefMarker)]
    stripped = _rebuilt_arguments(base, strip_markers)
    if kept:
        return Annotated[(stripped, *kept)]
    return stripped


def accepted_annotation(
    annotation: object,
    marker: RefMarker | None = None,
    *,
    discriminator: str | None = None,
    union_scalar_shorthand: UnionScalarShorthand | None = None,
) -> object:
    """``annotation`` as an operator may WRITE it: markers stripped, every
    model declaring a scalar shorthand widened to the union it accepts
    (``dict[str, EnvEntry]`` reads ``dict[str, str | EnvEntry]``), and an
    owner-templated reference widened to accept ``null``.

    Both widenings are the same correction, and it is what keeps the two
    derivations of a model saying the same thing. Each is a
    before-validator, which is invisible to an annotation and to
    ``model_json_schema`` alike; emitted schema learns it from
    :meth:`~agentworks.schema.AgwModel.__get_pydantic_json_schema__` and
    every human surface learns it here, from the same declaration.

    Left out, each has the same consequence, and both have shipped:
    ``describe-kind`` rendered a bare "table" for a field whose emitted
    schema offers ``{anyOf: [string, object]}``, so an operator who
    trusted the surface the resources guide calls the authority rewrote
    every plaintext env value into a table for no reason; and it rendered
    a bare "string" for a secret-naming field whose emitted schema offers
    ``{anyOf: [string, null]}`` and whose loader reads ``secret: null`` as
    the instruction to use the owner template. The parity guard
    (``tests/manifests/test_accepted_type_parity.py``) is what holds this
    function to the emitted side; ``null`` used to be subtracted there,
    which is precisely how the second one hid.

    The MODEL is not replaced, only the annotation naming it: a walker
    still expands the block through ``nested_model`` or ``item_model``,
    because a shorthand adds a spelling rather than removing one. The
    same goes for ``null``: an operator who writes it gets the templated
    name, so the field still names a Resource and still has a type.

    ``marker`` is the field's OWN reference marker, which is where the
    template lives; a collection's element marker cannot carry one
    (:func:`~agentworks.schema.reference_marker_error` refuses it at
    registration), so there is no depth for this widening to reach.
    """
    return _accepted_annotation(
        annotation,
        marker,
        (),
        discriminator=discriminator,
        union_scalar_shorthand=union_scalar_shorthand,
    )


def _accepted_annotation(
    annotation: object,
    marker: RefMarker | None,
    expanding_roots: tuple[type[BaseModel], ...],
    *,
    discriminator: str | None = None,
    union_scalar_shorthand: UnionScalarShorthand | None = None,
) -> object:
    """Implementation of :func:`accepted_annotation` with a root guard."""
    stripped = strip_markers(annotation)
    if discriminator is None:
        widened = _widened(stripped, expanding_roots)
    else:
        # A tagged union chooses its arm before an arm model validates.
        # Therefore an arm's own scalar shorthand does not widen the
        # union. Only the union-level dispatch declaration does.
        widened = stripped
        if union_scalar_shorthand is not None:
            resolved = union_scalar_resolution(stripped, union_scalar_shorthand)
            if not isinstance(resolved, str):
                shorthand, _tag = resolved
                widened = Union[shorthand.annotation, stripped]  # noqa: UP007
    if marker is None or marker.default_template is None:
        return widened
    return Union[widened, None]  # noqa: UP007


def element_annotation(annotation: object) -> object | None:
    """The type ONE element of a collection annotation holds, or ``None``
    when ``annotation`` does not hold a collection.

    For the presenter that has to name an element's own type: the flat
    stream has no doc for an element (a model says a list holds tables
    without saying how many), so the tree synthesizes one, and reading
    the type off the collection here is what keeps that synthesized node
    saying what :func:`accepted_annotation` already worked out rather
    than falling back to the element MODEL, which is one arm of it.
    """
    inner, _optional = unwrap_optional(annotation)
    inner, _metadata = split_annotated(inner)
    found = _collection_element(inner)
    return None if found is None else found[1]


def _widened(annotation: object, expanding_roots: tuple[type[BaseModel], ...]) -> object:
    """:func:`accepted_annotation`'s recursion, over an annotation whose
    markers are already stripped."""
    base, metadata = split_annotated(annotation)
    widened: object
    discriminator = _element_discriminator(metadata) if _is_model(base) or _is_union(base) else None
    if discriminator is not None:
        # A tagged union nested inside a collection keeps its dispatch
        # metadata on the element's Annotated wrapper. Treat it exactly
        # like a field-level tagged union: only the explicitly selected
        # arm contributes a scalar spelling. Recursing through each arm
        # here would infer every arm model's standalone shorthand even
        # though validation chooses an arm before running that shorthand.
        widened = _accepted_annotation(
            base,
            None,
            expanding_roots,
            discriminator=discriminator,
            union_scalar_shorthand=_sole_union_scalar_shorthand(metadata),
        )
    elif _is_model(base) and issubclass(base, RootModel) and base not in expanding_roots:
        fields = model_fields_of(base)
        root = fields.get("root") if fields is not None else None
        widened = base
        if root is not None:
            widened = _accepted_annotation(
                root.annotation,
                marker_of(root),
                (*expanding_roots, base),
                discriminator=_discriminator_of(root),
                union_scalar_shorthand=_sole_union_scalar_shorthand(spine_metadata(root)),
            )
    elif (shorthand := scalar_shorthand_of(base)) is not None:
        # The shorthand first, so the rendered type leads with the form
        # nearly every operator writes.
        widened = Union[shorthand.annotation, base]  # noqa: UP007
    else:
        widened = _rebuilt_arguments(base, lambda arg: _widened(arg, expanding_roots))
    if metadata:
        return Annotated[(widened, *metadata)]
    return widened


def _rebuilt_arguments(annotation: object, transform: Callable[[object], object]) -> object:
    """``annotation`` with ``transform`` applied to each of its type
    arguments, or unchanged when it has none or none of them moved.

    Shared by the two rewrites this module makes over an annotation TREE,
    so a shape one of them can rebuild is a shape both can. Annotations
    with no reconstructible origin (a ``Literal``, a callable) are
    returned unchanged.
    """
    args = get_args(annotation)
    if not args:
        return annotation
    rebuilt = tuple(transform(arg) for arg in args)
    if rebuilt == args:
        return annotation
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return Union[rebuilt]  # noqa: UP007
    if isinstance(origin, type):
        return types.GenericAlias(origin, rebuilt)
    return annotation


def _sole_model(members: tuple[object, ...]) -> type[BaseModel] | None:
    """The ONE model among an undiscriminated union's members, when
    exactly one of them is a model.

    Two or more and nothing here can choose: no tag addresses an arm from
    a raw blob, so naming one would be a guess, and rendering one would
    tell an operator that the arm the walk happened to meet first is the
    one they should write. Exactly one is the ``str | AccountRef`` shape,
    where the union offers a single block and some scalars that have
    nothing to expand.
    """
    models = [member for member in members if _is_model(member)]
    return models[0] if len(models) == 1 else None


def split_annotated(annotation: object) -> tuple[object, list[object]]:
    """``Annotated[X, a, b]`` to ``(X, [a, b])``; anything else unchanged."""
    metadata = getattr(annotation, "__metadata__", None)
    if metadata is None:
        return annotation, []
    return get_args(annotation)[0], list(metadata)


def unwrap_optional(annotation: object) -> tuple[object, bool]:
    """``X | None`` to ``(X, True)``. A union of two or more non-``None``
    arms is returned whole: it is a union, not an optional."""
    if not _is_union(annotation):
        return annotation, False
    args = get_args(annotation)
    present = tuple(arg for arg in args if arg is not type(None))
    if len(present) == len(args):
        return annotation, False
    if len(present) == 1:
        return present[0], True
    return Union[present], True  # noqa: UP007


def _unwrapped_annotation(annotation: object) -> tuple[object, bool, list[object]]:
    """Peel interleaved ``Annotated`` and optional wrappers at one position."""
    metadata: list[object] = []
    optional = False
    while True:
        annotation, found_metadata = split_annotated(annotation)
        metadata.extend(found_metadata)
        annotation, found_optional = unwrap_optional(annotation)
        optional = optional or found_optional
        if not found_metadata and not found_optional:
            return annotation, optional, metadata


def _is_union(annotation: object) -> bool:
    return get_origin(annotation) in (Union, types.UnionType)


def _is_model(annotation: object) -> TypeGuard[type[BaseModel]]:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _discriminator_of(field: FieldInfo) -> str | None:
    """The tag field name, from every spelling: ``Field(discriminator=)``
    or a ``Discriminator`` anywhere on the field's spine
    (:func:`spine_metadata`). A callable discriminator has no tag field
    to read from a raw blob, so it reads as undiscriminated here."""
    if isinstance(field.discriminator, str):
        return field.discriminator
    for candidate in (field.discriminator, *spine_metadata(field)):
        if isinstance(candidate, UnionScalarShorthand):
            return candidate.discriminator
        if isinstance(candidate, Discriminator) and isinstance(candidate.discriminator, str):
            return candidate.discriminator
    return None


def _element_discriminator(metadata: list[object]) -> str | None:
    """The tag field name a COLLECTION's elements are dispatched on.

    The element's own ``Annotated`` metadata is the only place it can be,
    which is why this reads a metadata list rather than a field the way
    :func:`_discriminator_of` does: a discriminator written on the FIELD
    selects among the FIELD's arms, and a field holding a collection has
    none of its own. Both spellings pydantic accepts inside an
    ``Annotated`` are read, because missing one is a silently
    unclassified union rather than an error.
    """
    for item in metadata:
        if isinstance(item, UnionScalarShorthand):
            return item.discriminator
        if isinstance(item, Discriminator) and isinstance(item.discriminator, str):
            return item.discriminator
        if isinstance(item, FieldInfo) and isinstance(item.discriminator, str):
            return item.discriminator
    return None


def _sole_union_scalar_shorthand(metadata: list[object]) -> UnionScalarShorthand | None:
    """The one scalar dispatch declaration in ``metadata``, if unique."""
    declarations = [item for item in metadata if isinstance(item, UnionScalarShorthand)]
    return declarations[0] if len(declarations) == 1 else None


def _arms_of(annotation: object, discriminator: str) -> tuple[UnionArmType, ...]:
    """The union's model arms paired with the tag that selects each.

    An arm whose discriminator field is not a single-string ``Literal``
    is skipped: nothing could address it from a raw blob.
    """
    arms: list[UnionArmType] = []
    # ``get_args`` is empty for the collapsed one-arm form, which IS the
    # annotation itself.
    members = get_args(annotation) or (annotation,)
    for arg in members:
        arm, _metadata = split_annotated(arg)
        if _is_model(arm):
            arms.extend(UnionArmType(tag=tag, model=arm) for tag in _tags_of(arm, discriminator))
    return tuple(arms)


def _tags_of(arm: type[BaseModel], discriminator: str) -> tuple[str, ...]:
    """Every tag value that selects ``arm``, in declaration order.

    An arm may answer to SEVERAL, which pydantic accepts and which a
    renamed capability keeping its old name would use
    (``Literal["aws-ec2", "ec2"]``). Reading only the first would leave
    the old name silently unaddressable.

    Non-string tags are out of scope, and that is a boundary rather than
    an oversight. The justification used to be that every discriminator
    here is a capability or kind NAME; that stopped being true when the
    platform auth and placement unions arrived tagged by ``mode``. The
    boundary stands on its own terms instead: a tag is an identifier the
    OPERATOR writes in a document, so it is a string, and a model tagged
    otherwise contributes no arms here.
    """
    fields = model_fields_of(arm)
    if fields is None or discriminator not in fields:
        # Pydantic refuses an arm with no discriminator field when the
        # union is declared, so this is reachable only for an arm that
        # cannot be built at all.
        return ()
    annotation, _optional = unwrap_optional(fields[discriminator].annotation)
    annotation, _metadata = split_annotated(annotation)
    if get_origin(annotation) is not typing.Literal:
        return ()
    return tuple(value for value in get_args(annotation) if isinstance(value, str))
