"""Render selected model fields using their declared interpolation eligibility."""

from __future__ import annotations

from typing import TYPE_CHECKING, overload

from agentworks.interpolation import render_template
from agentworks.schema.interpolation import field_interpolation

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel


@overload
def render_scalar_field(
    model: type[BaseModel],
    field_name: str,
    value: str,
    values: Mapping[str, str],
    *,
    location: str,
    legacy_placeholders: Mapping[str, str] | None = None,
    encode_inserted: Callable[[str], str] | None = None,
) -> str: ...


@overload
def render_scalar_field(
    model: type[BaseModel],
    field_name: str,
    value: None,
    values: Mapping[str, str],
    *,
    location: str,
    legacy_placeholders: Mapping[str, str] | None = None,
    encode_inserted: Callable[[str], str] | None = None,
) -> None: ...


def render_scalar_field(
    model: type[BaseModel],
    field_name: str,
    value: str | None,
    values: Mapping[str, str],
    *,
    location: str,
    legacy_placeholders: Mapping[str, str] | None = None,
    encode_inserted: Callable[[str], str] | None = None,
) -> str | None:
    """Render authored text at its consuming boundary; leave unmarked data literal.

    The declaring model supplies policy even when inheritance has moved the
    effective value into another carrier. Callers select applicable values and
    supply known context; neither declarations nor context are mutated.
    """
    if value is None or field_interpolation(model.model_fields[field_name]) != "scalar":
        return value
    return render_template(
        value,
        values,
        field=location,
        legacy_placeholders=legacy_placeholders,
        encode_inserted=encode_inserted,
    )


def render_list_field(
    model: type[BaseModel],
    field_name: str,
    value: list[str],
    values: Mapping[str, str],
    *,
    location: str,
    legacy_placeholders: Mapping[str, str] | None = None,
    encode_inserted: Callable[[str], str] | None = None,
) -> list[str]:
    """Render opted-in string elements without changing authored list order."""
    if field_interpolation(model.model_fields[field_name]) != "items":
        return list(value)
    return [
        render_template(
            item,
            values,
            field=f"{location}[{index}]",
            legacy_placeholders=legacy_placeholders,
            encode_inserted=encode_inserted,
        )
        for index, item in enumerate(value)
    ]
