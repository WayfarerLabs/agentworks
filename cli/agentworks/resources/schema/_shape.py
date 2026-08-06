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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, TypeGuard, Union, get_args, get_origin

from pydantic import BaseModel, Discriminator

from agentworks.resources.schema.markers import RefMarker

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo


@dataclass(frozen=True)
class UnionArmType:
    """One arm of a discriminated union: the tag that selects it and the
    model it selects."""

    tag: str
    model: type[BaseModel]


@dataclass(frozen=True, kw_only=True)
class FieldShape:
    """The classified shape of one declared field.

    At most one of ``marker`` / ``item_marker`` / ``nested_model`` /
    ``arms`` is meaningful; a field that is none of them is an ordinary
    scalar, which both walkers treat as carrying no references.
    """

    annotation: object
    """The field's annotation with reference markers stripped out, at
    every depth. Optionality is preserved: ``str | None`` stays
    ``str | None``."""

    optional: bool
    """Whether ``None`` is an accepted value."""

    marker: RefMarker | None
    """The marker on the field itself: the field names one Resource."""

    item_marker: RefMarker | None
    """The marker on a list's elements: the field names many Resources."""

    nested_model: type[BaseModel] | None
    """The model this field opens a nested block of."""

    discriminator: str | None
    """The field name carrying a discriminated union's tag."""

    arms: tuple[UnionArmType, ...]
    """The union's arms, in declaration order; empty unless the field is
    a discriminated union of models."""


def marker_of(field: FieldInfo) -> RefMarker | None:
    """The reference marker on the field ITSELF, if any.

    Both spellings are found: ``Annotated[str | None, SecretRef(...)]``,
    where pydantic lifts the marker into ``field.metadata``, and
    ``Annotated[str, SecretRef(...)] | None``, where it stays inside the
    annotation. Cheaper than a whole :func:`shape_of` for the callers
    that only want the marker.
    """
    inner, _optional = unwrap_optional(field.annotation)
    _inner, inner_metadata = _split_annotated(inner)
    return _first_marker([*field.metadata, *inner_metadata])


def shape_of(field: FieldInfo) -> FieldShape:
    """Classify ``field``. Reads annotations only; runs no user code."""
    inner, optional = unwrap_optional(field.annotation)
    inner, _inner_metadata = _split_annotated(inner)
    marker = marker_of(field)

    item_marker: RefMarker | None = None
    nested_model: type[BaseModel] | None = None
    discriminator: str | None = None
    arms: tuple[UnionArmType, ...] = ()

    if get_origin(inner) is list:
        args = get_args(inner)
        if args:
            _item, item_metadata = _split_annotated(args[0])
            item_marker = _first_marker(item_metadata)
    elif _is_model(inner):
        nested_model = inner
    elif _is_union(inner):
        discriminator = _discriminator_of(field)
        if discriminator is not None:
            arms = _arms_of(inner, discriminator)

    return FieldShape(
        annotation=strip_markers(field.annotation),
        optional=optional,
        marker=marker,
        item_marker=item_marker,
        nested_model=nested_model,
        discriminator=discriminator,
        arms=arms,
    )


def model_fields_of(model_cls: type[BaseModel]) -> dict[str, FieldInfo] | None:
    """``model_cls``'s fields, or ``None`` when the model cannot be built.

    A model with an unresolved forward reference has no usable fields.
    One rebuild attempt is made (the annotation may have become
    resolvable since class definition); a model still incomplete after
    it is reported as unusable rather than raising, and each caller
    decides what that means for it.
    """
    if not model_cls.__pydantic_complete__:
        model_cls.model_rebuild(raise_errors=False)
        if not model_cls.__pydantic_complete__:
            return None
    return dict(model_cls.model_fields)


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
    """The tag field name, from either spelling: ``Field(discriminator=)``
    or ``Annotated[A | B, Discriminator("name")]``. A callable
    discriminator has no tag field to read from a raw blob, so it reads
    as undiscriminated here."""
    if isinstance(field.discriminator, str):
        return field.discriminator
    for candidate in (field.discriminator, *field.metadata):
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
            tag = _tag_of(arm, discriminator)
            if tag is not None:
                arms.append(UnionArmType(tag=tag, model=arm))
    return tuple(arms)


def _tag_of(arm: type[BaseModel], discriminator: str) -> str | None:
    fields = model_fields_of(arm)
    if fields is None or discriminator not in fields:
        return None
    annotation, _optional = unwrap_optional(fields[discriminator].annotation)
    annotation, _metadata = _split_annotated(annotation)
    if get_origin(annotation) is not typing.Literal:
        return None
    values = get_args(annotation)
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0]
