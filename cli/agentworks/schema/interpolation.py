"""Declared runtime interpolation metadata and its supported model shapes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, get_args, get_origin

from agentworks.schema._shape import (
    element_metadata,
    model_fields_of,
    models_in,
    shape_of,
    spine_metadata,
    split_annotated,
    unwrap_optional,
)
from agentworks.schema.markers import Interpolated, RefMarker

if TYPE_CHECKING:
    from pydantic import BaseModel
    from pydantic.fields import FieldInfo

InterpolationTarget = Literal["scalar", "items"]


def field_interpolation(field: FieldInfo) -> InterpolationTarget | None:
    """Where a field declares interpolation, independent of invocation context."""
    if any(isinstance(item, Interpolated) for item in spine_metadata(field)):
        return "scalar"
    if any(isinstance(item, Interpolated) for item in element_metadata(field)):
        return "items"
    return None


def interpolation_marker_error(model: type[BaseModel]) -> str | None:
    """Reject model metadata an external component's declared contract cannot honor.

    Registration is the boundary for component models supplied outside first-party
    typing. Shared model bases also call this when a declaration is first built.
    """
    return _model_error(model, ())


def _model_error(model: type[BaseModel], visiting: tuple[type[BaseModel], ...]) -> str | None:
    if model in visiting or not model.model_fields:
        return None
    fields = model_fields_of(model)
    if fields is None:
        return None
    visiting = (*visiting, model)
    for name, field in fields.items():
        error = _value_error(field.annotation, field.metadata, depth=0)
        if error is not None:
            return f"{model.__name__}.{name}: {error}"
        shape = shape_of(field)
        reachable = tuple(
            child
            for child in (
                shape.block,
                shape.item_block,
                *(arm.model for arm in (*shape.arms, *shape.item_arms)),
                *shape.structural_arms,
                *shape.item_structural_arms,
            )
            if child is not None
        )
        for child in models_in(field.annotation):
            if child not in reachable and _has_interpolation(child, ()):
                return f"{model.__name__}.{name}: interpolation is hidden in an unsupported model shape"
            error = _model_error(child, visiting)
            if error is not None:
                return error
    return None


def _value_error(annotation: object, metadata: list[object], *, depth: int) -> str | None:
    base, attached = split_annotated(annotation)
    metadata = [*metadata, *attached]
    present, optional = unwrap_optional(base)
    if optional:
        return _value_error(present, metadata, depth=depth)
    markers = [item for item in metadata if isinstance(item, Interpolated)]
    if markers:
        if len(markers) != 1 or base is not str or depth > 1:
            return "interpolation must annotate a string field or one string list element"
        if any(isinstance(item, RefMarker) for item in metadata):
            return "interpolation cannot also annotate a resource or secret reference"
        return None
    if get_origin(base) is list and depth == 0:
        return _value_error(get_args(base)[0], [], depth=1)
    if _markers_in(base):
        return "interpolation is declared in an unsupported position"
    return None


def _markers_in(annotation: object) -> bool:
    base, metadata = split_annotated(annotation)
    if any(isinstance(item, Interpolated) for item in metadata):
        return True
    return any(_markers_in(arg) for arg in get_args(base))


def _has_interpolation(model: type[BaseModel], visiting: tuple[type[BaseModel], ...]) -> bool:
    if model in visiting or not model.model_fields:
        return False
    fields = model_fields_of(model)
    if fields is None:
        return False
    visiting = (*visiting, model)
    return any(
        field_interpolation(field) is not None
        or _markers_in(field.annotation)
        or any(_has_interpolation(child, visiting) for child in models_in(field.annotation))
        for field in fields.values()
    )
