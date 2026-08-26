"""Shared mechanics for domain-owned instance-overlay codecs."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import ValidationError as PydanticValidationError

from agentworks.declared_resource import DeclaredResource
from agentworks.errors import ValidationError

if TYPE_CHECKING:
    from pydantic import BaseModel

    from agentworks.db.instance_state import JsonObject

OVERLAY_EXCLUDED_FIELDS = frozenset(DeclaredResource.model_fields) | {
    "apiVersion",
    "framework",
    "kind",
    "inherits",
    "metadata",
    "source",
    "spec",
}
"""Fields that are framework or envelope surface, never an instance layer.

The declared-resource base is the authority for model metadata. The remaining
names are document wrappers that are not model fields, plus ``inherits``, whose
composition belongs to the selected template.
"""


class UnsupportedOverlayFieldsError(ValidationError):
    """An overlay uses declaration fields this release does not understand."""


def decode_overlay_model[T: BaseModel](model_type: type[T], instance_kind: str, raw: JsonObject) -> T:
    """Validate an overlay without exposing operator values in failures."""
    try:
        return model_type.model_validate({"name": "instance-overlay", **raw})
    except PydanticValidationError as error:
        errors = error.errors(include_url=False, include_context=False, include_input=False)
        details = []
        errors_by_parent: dict[tuple[object, ...], set[str]] = {}
        for item in errors:
            location = ".".join(str(part) for part in item["loc"] if part != "name") or "<root>"
            details.append(f"{location}: {item['msg']}")
            errors_by_parent.setdefault(item["loc"][:-1], set()).add(item["type"])
        error_type = (
            UnsupportedOverlayFieldsError
            if any(item["type"] == "extra_forbidden" and len(item["loc"]) == 1 for item in errors)
            or any(types == {"extra_forbidden"} for types in errors_by_parent.values())
            else ValidationError
        )
        raise error_type(
            f"invalid {instance_kind} instance spec: {'; '.join(details)}",
            entity_kind=instance_kind,
        ) from None


def encode_overlay_model(model: BaseModel, instance_kind: str) -> JsonObject:
    """Project only explicitly supplied domain fields to canonical JSON data."""
    try:
        return cast(
            "JsonObject",
            model.model_dump(
                mode="json",
                exclude=set(OVERLAY_EXCLUDED_FIELDS),
                exclude_unset=True,
            ),
        )
    except ValueError:
        raise ValidationError(
            f"invalid {instance_kind} instance spec: configuration is too deeply nested to serialize",
            entity_kind=instance_kind,
        ) from None
