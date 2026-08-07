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
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Final, TypeGuard, Union, get_args, get_origin

from pydantic import BaseModel, Discriminator
from pydantic.errors import PydanticSchemaGenerationError, PydanticUndefinedAnnotation
from pydantic.fields import FieldInfo
from pydantic.json_schema import SkipJsonSchema

from agentworks.schema.markers import RefMarker
from agentworks.schema.shorthand import scalar_shorthand_of

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
    discriminated union (``arms``), or an ordinary scalar, which both
    walkers treat as carrying no references.
    """

    annotation: object
    """What an operator may WRITE here, as an annotation: reference
    markers stripped out at every depth, and every model declaring a
    scalar shorthand widened to the union it accepts (``EnvEntry`` reads
    ``str | EnvEntry``). Optionality is preserved: ``str | None`` stays
    ``str | None``. See :func:`accepted_annotation`."""

    optional: bool
    """Whether ``None`` is an accepted value."""

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
    today (all four discriminated unions are top-level capability
    configs), and it is one any capability or plugin author can write.
    Left unclassified, its elements read as an undiscriminated union,
    which no walker expands: a secret named inside such an element would
    be absent from the dependency graph with nothing reported."""

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
    inner, _optional = unwrap_optional(field.annotation)
    inner, _metadata = split_annotated(inner)
    found = _collection_element(inner)
    if found is None:
        return []
    _kind, element = found
    _element, metadata = split_annotated(element)
    return metadata


def marker_of(field: FieldInfo) -> RefMarker | None:
    """The reference marker on the field ITSELF, if any. Cheaper than a
    whole :func:`shape_of` for the callers that only want the marker."""
    return _first_marker(spine_metadata(field))


def shape_of(field: FieldInfo) -> FieldShape:
    """Classify ``field``. Reads annotations only; runs no user code."""
    inner, optional = unwrap_optional(field.annotation)
    inner, _inner_metadata = split_annotated(inner)
    marker = marker_of(field)

    collection: Collection | None = None
    item_marker: RefMarker | None = None
    item_model: type[BaseModel] | None = None
    nested_model: type[BaseModel] | None = None
    discriminator: str | None = None
    arms: tuple[UnionArmType, ...] = ()
    item_discriminator: str | None = None
    item_arms: tuple[UnionArmType, ...] = ()
    union_members: tuple[object, ...] = ()
    union_model: type[BaseModel] | None = None
    item_union_members: tuple[object, ...] = ()
    item_union_model: type[BaseModel] | None = None

    found = _collection_element(inner)
    if found is not None:
        collection, element = found
        element, element_meta = split_annotated(element)
        item_marker = _first_marker(element_meta)
        item_model = element if _is_model(element) else None
        if item_model is None and _is_union(element):
            # Same order as the field-level branch below, and for the same
            # reason: a tagged union addresses one arm from a raw blob and
            # an untagged one addresses none, so asking about the tag first
            # is what keeps a collection of tagged blocks from reading as
            # an opaque union nothing walks into.
            item_discriminator = _element_discriminator(element_meta)
            if item_discriminator is not None:
                item_arms = _arms_of(element, item_discriminator)
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
        elif _is_model(inner):
            nested_model = inner
        else:
            union_members = tuple(split_annotated(arg)[0] for arg in get_args(inner))
            union_model = _sole_model(union_members)

    return FieldShape(
        annotation=accepted_annotation(field.annotation),
        optional=optional,
        marker=marker,
        collection=collection,
        item_marker=item_marker,
        item_model=item_model,
        nested_model=nested_model,
        discriminator=discriminator,
        arms=arms,
        item_discriminator=item_discriminator,
        item_arms=item_arms,
        union_members=union_members,
        union_model=union_model,
        item_union_members=item_union_members,
        item_union_model=item_union_model,
    )


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


def is_model(annotation: object) -> TypeGuard[type[BaseModel]]:
    """Whether ``annotation`` is a model class. Shared so the bridge asks
    the same question this module does."""
    return _is_model(annotation)


def accepts_table(annotation: object) -> bool:
    """Whether a raw TABLE could satisfy ``annotation``.

    Reference extraction asks this of an undiscriminated union's members,
    because being a table is the only thing that selects such a union's
    block from a raw blob: no tag names it. The answer is a fact only
    while the block is the ONE member a table could be, so the extractor
    asks after the others (see ``extract._union_block``).

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

    A fixed-length heterogeneous tuple (``tuple[str, int]``) is not a
    collection here: it has no single element type, and it is not a shape
    an operator writes in YAML.
    """
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is dict:
        return (Collection.MAPPING, args[1]) if len(args) == 2 else None
    if origin is tuple:
        return (Collection.SEQUENCE, args[0]) if len(args) == 2 and args[1] is Ellipsis else None
    if origin in (list, set, frozenset):
        return (Collection.SEQUENCE, args[0]) if args else None
    return None


def _first_marker(metadata: list[object]) -> RefMarker | None:
    for item in metadata:
        if isinstance(item, RefMarker):
            return item
    return None


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


def accepted_annotation(annotation: object) -> object:
    """``annotation`` as an operator may WRITE it: markers stripped, and
    every model declaring a scalar shorthand widened to the union it
    accepts (``dict[str, EnvEntry]`` reads ``dict[str, str | EnvEntry]``).

    The widening is what keeps the two derivations of a model saying the
    same thing. A shorthand is a before-validator, which is invisible to
    an annotation and to ``model_json_schema`` alike; emitted schema
    learns it from
    :meth:`~agentworks.schema.AgwModel.__get_pydantic_json_schema__` and
    every human surface learns it here, from the same declaration. Left
    out, ``describe-kind`` renders a bare "table" for a field whose
    emitted schema offers ``{anyOf: [string, object]}``, and an operator
    who trusts the surface the resources guide calls the authority
    rewrites every plaintext env value into a table for no reason.

    The MODEL is not replaced, only the annotation naming it: a walker
    still expands the block through ``nested_model`` or ``item_model``,
    because a shorthand adds a spelling rather than removing one.
    """
    return _widened(strip_markers(annotation))


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


def _widened(annotation: object) -> object:
    """:func:`accepted_annotation`'s recursion, over an annotation whose
    markers are already stripped."""
    base, metadata = split_annotated(annotation)
    shorthand = scalar_shorthand_of(base)
    if shorthand is not None:
        # The shorthand first, so the rendered type leads with the form
        # nearly every operator writes.
        widened: object = Union[shorthand.annotation, base]  # noqa: UP007
    else:
        widened = _rebuilt_arguments(base, _widened)
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
        if isinstance(item, Discriminator) and isinstance(item.discriminator, str):
            return item.discriminator
        if isinstance(item, FieldInfo) and isinstance(item.discriminator, str):
            return item.discriminator
    return None


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
    an oversight: every discriminator in this framework is a capability
    or kind NAME. A model tagged otherwise contributes no arms here.
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
