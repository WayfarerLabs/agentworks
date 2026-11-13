"""Render winning plaintext env entries without resolving secret-backed entries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.env.entry import PlaintextEnvEntry
from agentworks.errors import ValidationError
from agentworks.interpolation_compat import (
    LEGACY_PLACEHOLDERS,
    uses_legacy_placeholders,
    warn_legacy_placeholders,
)
from agentworks.interpolation_fields import render_scalar_field

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from agentworks.env.entry import EnvEntry


def render_plaintext(
    entries: Mapping[str, EnvEntry],
    values: Mapping[str, str],
    *,
    field: str = "env",
    legacy_keys: Collection[str] = (),
) -> dict[str, str]:
    """Render the plaintext part of an already-merged env map.

    Authored env values are template input. Secret names and contents do not
    enter this renderer. Callers may use it before any secret resolution to
    validate an invocation, and use the same result at display or execution.
    ``legacy_keys`` identifies the session declarations. Entries have already
    been merged and filtered for managed identity, so discarded keys are never
    inspected for compatibility.
    """
    rendered: dict[str, str] = {}
    for key, entry in entries.items():
        if entry.value is None:
            continue
        location = f"{field}.{key}"
        legacy = key in legacy_keys
        value = render_scalar_field(
            PlaintextEnvEntry,
            "value",
            entry.value,
            values,
            location=location,
            legacy_placeholders=LEGACY_PLACEHOLDERS if legacy else None,
        )
        if any(character in value for character in ("\0", "\r", "\n")):
            raise ValidationError(
                f"{location}: environment value contains an unsupported line separator or NUL",
                entity_kind="template",
                entity_name=location,
            )
        if legacy and uses_legacy_placeholders(entry.value):
            warn_legacy_placeholders()
        rendered[key] = value
    return rendered
