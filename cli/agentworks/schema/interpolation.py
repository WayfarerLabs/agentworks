"""Declared runtime interpolation metadata and its supported model shapes."""

from __future__ import annotations

from typing import TYPE_CHECKING, ForwardRef, Literal, TypeAliasType, get_args, get_origin

from pydantic import BaseModel

# The backport remains a distinct runtime type on Python 3.12.
from typing_extensions import TypeAliasType as BackportTypeAliasType  # noqa: UP035

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
    from collections.abc import Iterator

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
    schema: dict[str, object] | None = None
    for name, field in fields.items():
        error = _value_error(field.annotation, field.metadata, depth=0)
        if error is not None:
            return f"{model.__name__}.{name}: {error}"
        if any(item is _OPAQUE_ALIAS for item in _declaration_metadata(field.annotation)):
            if schema is None:
                schema = model.model_json_schema(by_alias=False)
            property_name = None if model.__pydantic_root_model__ else name
            if _schema_has_interpolation(schema, schema, property_name=property_name):
                return f"{model.__name__}.{name}: interpolation is hidden in an unsupported type alias"
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
        if depth and (_markers_in(present) or any(isinstance(item, Interpolated) for item in metadata)):
            return "interpolated list elements must be non-null strings"
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


_OPAQUE_ALIAS = object()


def _markers_in(annotation: object) -> bool:
    return any(isinstance(item, Interpolated) for item in _declaration_metadata(annotation))


def _declaration_metadata(
    annotation: object, visiting: frozenset[int] = frozenset(), *, inside_alias: bool = False
) -> Iterator[object]:
    """Read alias contents for refusal, without resolving forward references."""
    base, metadata = split_annotated(annotation)
    yield from metadata
    if id(base) in visiting:
        return
    visiting = visiting | {id(base)}
    alias = get_origin(base) or base
    if isinstance(alias, TypeAliasType | BackportTypeAliasType):
        try:
            target = alias.__value__
        except NameError:
            yield _OPAQUE_ALIAS
        else:
            if isinstance(target, str | ForwardRef):
                yield _OPAQUE_ALIAS
            else:
                yield from _declaration_metadata(target, visiting, inside_alias=True)
    elif inside_alias and isinstance(base, type) and issubclass(base, BaseModel):
        for field in (model_fields_of(base) or {}).values():
            yield from field.metadata
            yield from _declaration_metadata(field.annotation, visiting, inside_alias=True)
    for arg in get_args(base):
        yield from _declaration_metadata(arg, visiting, inside_alias=inside_alias)


def _schema_has_interpolation(
    node: object,
    root: dict[str, object],
    *,
    property_name: str | None,
    references: frozenset[str] = frozenset(),
) -> bool:
    """Inspect one completed field's schema only when an alias hides its source."""
    if isinstance(node, list):
        return any(
            _schema_has_interpolation(child, root, property_name=property_name, references=references) for child in node
        )
    if not isinstance(node, dict):
        return False
    reference = node.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/") and reference not in references:
        target: object = root
        for part in reference[2:].split("/"):
            target = target.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(target, dict) else None
        if _schema_has_interpolation(target, root, property_name=property_name, references=references | {reference}):
            return True
    if property_name is not None:
        properties = node.get("properties", {})
        if isinstance(properties, dict) and _schema_has_interpolation(
            properties.get(property_name), root, property_name=None, references=references
        ):
            return True
        children = (node.get(key) for key in ("anyOf", "oneOf", "allOf"))
    else:
        if node.get("x-agw-interpolation") is True:
            return True
        children = (
            value for key, value in node.items() if key not in {"$defs", "$ref", "examples", "default", "enum", "const"}
        )
    return any(
        _schema_has_interpolation(child, root, property_name=property_name, references=references) for child in children
    )


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
