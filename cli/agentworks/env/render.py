"""Render winning plaintext env entries without resolving secret-backed entries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.interpolation import render_template

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.env.entry import EnvEntry


def render_plaintext(
    entries: Mapping[str, EnvEntry], values: Mapping[str, str], *, field: str = "env"
) -> dict[str, str]:
    """Render the plaintext part of an already-merged env map.

    Authored env values are template input. Secret names and contents do not
    enter this renderer. Callers may use it before any secret resolution to
    validate an invocation, and use the same result at display or execution.
    """
    rendered: dict[str, str] = {}
    for key, entry in entries.items():
        if entry.value is None:
            continue
        location = f"{field}.{key}"
        value = render_template(entry.value, values, field=location)
        if any(character in value for character in ("\0", "\r", "\n")):
            raise ValidationError(
                f"{location}: environment value contains an unsupported line separator or NUL",
                entity_kind="template",
                entity_name=location,
            )
        rendered[key] = value
    return rendered
