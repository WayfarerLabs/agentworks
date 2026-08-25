"""Shared mechanics for domain-owned instance-overlay codecs."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import ValidationError as PydanticValidationError

from agentworks.errors import ValidationError

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.db.instance_state import JsonObject

_MODEL_EXCLUDED_FIELDS = {
    "name",
    "inherits",
    "description",
    "expires",
    "declared_at",
    "origin",
}


def decode_overlay_model[T: BaseModel](model_type: type[T], instance_kind: str, raw: JsonObject) -> T:
    """Validate an overlay without exposing operator values in failures."""
    try:
        return model_type.model_validate({"name": "instance-overlay", **raw})
    except PydanticValidationError as error:
        details = []
        for item in error.errors(include_url=False, include_context=False, include_input=False):
            location = ".".join(str(part) for part in item["loc"] if part != "name") or "<root>"
            details.append(f"{location}: {item['msg']}")
        raise ValidationError(
            f"invalid {instance_kind} instance spec: {'; '.join(details)}",
            entity_kind=instance_kind,
        ) from None


def encode_overlay_model(model: BaseModel) -> JsonObject:
    """Project only explicitly supplied domain fields to canonical JSON data."""
    return cast(
        "JsonObject",
        model.model_dump(
            mode="json",
            exclude=_MODEL_EXCLUDED_FIELDS,
            exclude_unset=True,
        ),
    )
