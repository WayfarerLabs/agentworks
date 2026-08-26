"""Shared mechanics for domain-owned instance-overlay codecs."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import ValidationError as PydanticValidationError

from agentworks.declared_resource import DeclaredResource
from agentworks.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

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


def decode_overlay_model[T: BaseModel](
    model_type: type[T],
    instance_kind: str,
    raw: JsonObject,
    *,
    validation_context: dict[str, bool] | None = None,
    entity_kind: str | None = None,
) -> T:
    """Validate an overlay without exposing operator values in failures."""
    try:
        return model_type.model_validate(
            {"name": "instance-overlay", **raw},
            context=validation_context,
        )
    except PydanticValidationError as error:
        raise value_safe_model_validation_error(
            error,
            f"invalid {instance_kind} instance spec",
            entity_kind=entity_kind or instance_kind,
        ) from None


def value_safe_model_validation_error(
    error: PydanticValidationError,
    label: str,
    *,
    entity_kind: str,
    entity_name: str | None = None,
    classify_unsupported: bool = True,
    custom_message_sanitizer: Callable[[str], str | None] | None = None,
) -> ValidationError:
    """Translate Pydantic failures without including operator-supplied values."""
    errors = error.errors(include_url=False, include_context=False, include_input=False)
    details = []
    errors_by_parent: dict[tuple[object, ...], set[str]] = {}
    for item in errors:
        location = ".".join(str(part) for part in item["loc"] if part != "name") or "<root>"
        message = item["msg"]
        if item["type"] in {"value_error", "assertion_error"}:
            sanitized = None if custom_message_sanitizer is None else custom_message_sanitizer(message)
            message = sanitized or "Value failed domain validation"
        details.append(f"{location}: {message}")
        errors_by_parent.setdefault(item["loc"][:-1], set()).add(item["type"])
    unsupported = classify_unsupported and (
        any(item["type"] == "extra_forbidden" and len(item["loc"]) == 1 for item in errors)
        or any(types == {"extra_forbidden"} for types in errors_by_parent.values())
    )
    error_type = UnsupportedOverlayFieldsError if unsupported else ValidationError
    return error_type(
        f"{label}: {'; '.join(details)}",
        entity_kind=entity_kind,
        entity_name=entity_name,
    )


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
