"""Shared projection from typed template declarations to resolved layers."""

from __future__ import annotations

from dataclasses import MISSING, Field, fields, replace
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.resources.inheritance import LayerContribution


def merge_resolved_template_layer[T](
    target: T,
    declaration: BaseModel,
    _source: object,
) -> tuple[T, tuple[LayerContribution, ...]]:
    """Merge one declaration onto a dataclass-backed resolved template.

    The declaration and resolved forms intentionally need not expose identical
    fields. Framework-only declaration fields stay outside the resolved value,
    while resolved-only defaults remain on the accumulator. Shared fields are
    merged according to the declaration model, with ``None`` retaining its
    domain meaning of absence.
    """
    from agentworks.env.entry import EnvEntry
    from agentworks.instance_overlay_codec import OVERLAY_EXCLUDED_FIELDS
    from agentworks.schema import merge_model

    declaration_fields = type(declaration).model_fields
    merge_fields = tuple(
        field
        for field in fields(cast("Any", target))
        if field.name in declaration_fields and field.name not in OVERLAY_EXCLUDED_FIELDS
    )
    merge_field_names = frozenset(field.name for field in merge_fields)
    previous = {field.name: getattr(target, field.name) for field in merge_fields}
    if "env" in merge_field_names:
        previous["env"] = {
            key: entry.model_dump(mode="python") for key, entry in cast("dict[str, EnvEntry]", previous["env"]).items()
        }
    dumped = declaration.model_dump(
        mode="python",
        exclude=set(OVERLAY_EXCLUDED_FIELDS),
        exclude_unset=True,
    )
    authored = {name: value for name, value in dumped.items() if name in merge_field_names and value is not None}
    merged, operations = merge_model(type(declaration), previous, authored)
    raw = cast("dict[str, object]", merged)
    for field in merge_fields:
        if field.name not in raw:
            raw[field.name] = _field_default(field)
    if "env" in raw:
        raw["env"] = {
            key: EnvEntry.model_validate(value) for key, value in cast("dict[str, object]", raw["env"]).items()
        }
    return cast("T", replace(cast("Any", target), **raw)), operations


def _field_default(field: Field[Any]) -> object:
    """Materialize one resolved dataclass default after root replacement."""
    if field.default is not MISSING:
        return field.default
    if field.default_factory is not MISSING:
        return field.default_factory()
    raise AssertionError(f"shared resolved field {field.name!r} has no default")
