"""What a declared field IS, read off its annotation.

Both walkers in this package (reference extraction and the
field-reference stream) ask the same questions of a field: is it marked
as a reference, is it a list of them, does it open a nested block, is it
a discriminated union? Asking them once, here, is what keeps the two
walkers from drifting apart on a shape.

Internal to the package: the public surface is
``resources/schema/__init__``.
"""

from __future__ import annotations

import types
import typing
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Annotated, TypeGuard, Union, get_args, get_origin

from pydantic import BaseModel, Discriminator
from pydantic.errors import PydanticSchemaGenerationError, PydanticUndefinedAnnotation

from agentworks.resources.schema.markers import RefMarker

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo


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
    """The field's annotation with reference markers stripped out, at
    every depth. Optionality is preserved: ``str | None`` stays
    ``str | None``."""

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

    union_members: tuple[object, ...]
    """The members of an UNDISCRIMINATED union, in declaration order, as
    raw annotations. Empty unless the field is a union with no
    discriminator (``str | AccountRef``, a secret backend's mapping).

    Neither walker reads this: an undiscriminated union names no single
    arm from a raw blob, so it contributes no references and does not
    expand. The error bridge reads it, because pydantic reports one error
    per member and prefixes each with the member's own name, a segment
    the operator never wrote."""


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
    base, metadata = _split_annotated(field.annotation)
    items.extend(metadata)
    if _is_union(base):
        for arg in get_args(base):
            _arm, arm_metadata = _split_annotated(arg)
            items.extend(arm_metadata)
    return items


def element_metadata(field: FieldInfo) -> list[object]:
    """Every ``Annotated`` metadata item on the elements of a collection
    field, or nothing when the field holds a single value."""
    inner, _optional = unwrap_optional(field.annotation)
    inner, _metadata = _split_annotated(inner)
    found = _collection_element(inner)
    if found is None:
        return []
    _kind, element = found
    _element, metadata = _split_annotated(element)
    return metadata


def marker_of(field: FieldInfo) -> RefMarker | None:
    """The reference marker on the field ITSELF, if any. Cheaper than a
    whole :func:`shape_of` for the callers that only want the marker."""
    return _first_marker(spine_metadata(field))


def shape_of(field: FieldInfo) -> FieldShape:
    """Classify ``field``. Reads annotations only; runs no user code."""
    inner, optional = unwrap_optional(field.annotation)
    inner, _inner_metadata = _split_annotated(inner)
    marker = marker_of(field)

    collection: Collection | None = None
    item_marker: RefMarker | None = None
    item_model: type[BaseModel] | None = None
    nested_model: type[BaseModel] | None = None
    discriminator: str | None = None
    arms: tuple[UnionArmType, ...] = ()
    union_members: tuple[object, ...] = ()

    found = _collection_element(inner)
    if found is not None:
        collection, element = found
        element, element_meta = _split_annotated(element)
        item_marker = _first_marker(element_meta)
        item_model = element if _is_model(element) else None
    elif _is_model(inner):
        nested_model = inner
    elif _is_union(inner):
        discriminator = _discriminator_of(field)
        if discriminator is not None:
            arms = _arms_of(inner, discriminator)
        else:
            union_members = tuple(_split_annotated(arg)[0] for arg in get_args(inner))

    return FieldShape(
        annotation=strip_markers(field.annotation),
        optional=optional,
        marker=marker,
        collection=collection,
        item_marker=item_marker,
        item_model=item_model,
        nested_model=nested_model,
        discriminator=discriminator,
        arms=arms,
        union_members=union_members,
    )


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
    base, metadata = _split_annotated(annotation)
    kept = [item for item in metadata if not isinstance(item, RefMarker)]
    stripped = _strip_arguments(base)
    if kept:
        return Annotated[(stripped, *kept)]
    return stripped


def _strip_arguments(annotation: object) -> object:
    args = get_args(annotation)
    if not args:
        return annotation
    rebuilt = tuple(strip_markers(arg) for arg in args)
    if rebuilt == args:
        return annotation
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        return Union[rebuilt]  # noqa: UP007
    if isinstance(origin, type):
        return types.GenericAlias(origin, rebuilt)
    return annotation


def _split_annotated(annotation: object) -> tuple[object, list[object]]:
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


def _arms_of(annotation: object, discriminator: str) -> tuple[UnionArmType, ...]:
    """The union's model arms paired with the tag that selects each.

    An arm whose discriminator field is not a single-string ``Literal``
    is skipped: nothing could address it from a raw blob.
    """
    arms: list[UnionArmType] = []
    for arg in get_args(annotation):
        arm, _metadata = _split_annotated(arg)
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
    annotation, _metadata = _split_annotated(annotation)
    if get_origin(annotation) is not typing.Literal:
        return ()
    return tuple(value for value in get_args(annotation) if isinstance(value, str))
