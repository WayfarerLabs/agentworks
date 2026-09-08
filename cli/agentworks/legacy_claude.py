"""Finite conversion of persisted Claude fields to explicit user attachments.

Only agent payload v1 and the admin component of VM payload v2 are recognized.
Authored declarations never pass through this adapter. Callers supply the owning
resolved template's attachments, before applying the stored instance layer.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from agentworks.db.instance_state import VersionedPayload
from agentworks.errors import StateError

if TYPE_CHECKING:
    from agentworks.db import Database, DesiredOverlayRecord
    from agentworks.db.instance_state import JsonObject, JsonValue
    from agentworks.schema import CapabilityBlock

LEGACY_CLAUDE_FIELDS = frozenset({"claude_marketplaces", "claude_plugins"})


class LegacyClaudeContextRequired(StateError):
    """Valid stored legacy fields need their selected template to translate."""


def legacy_component(record: DesiredOverlayRecord) -> JsonObject | None:
    """Recognize the finite supported persisted envelopes without guessing versions."""
    if record.instance_kind == "agent" and record.payload.payload_version == 1:
        raw = record.payload.value
    elif record.instance_kind == "vm" and record.payload.payload_version == 2:
        component = record.payload.value.get("admin")
        if not isinstance(component, dict):
            return None
        raw = component
    else:
        return None
    return raw if LEGACY_CLAUDE_FIELDS.intersection(raw) else None


def translate_component(raw: JsonObject, base: list[CapabilityBlock] | None, *, agent: bool) -> JsonObject:
    """Validate legacy values, then combine them with the complete resolved base.

    With no context, return only the stripped component so the normal codec can
    validate unrelated fields before reporting that contextual migration waits.
    """
    if "harness_integrations" in raw:
        raise StateError("legacy Claude fields conflict with harness_integrations in the same stored component")
    legacy: dict[str, list[str]] = {}
    for field in sorted(LEGACY_CLAUDE_FIELDS.intersection(raw)):
        value = raw[field]
        if value is None and agent:
            continue
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise StateError(f"stored {field} must be a list of strings")
        legacy[field.removeprefix("claude_")] = cast("list[str]", value)
    result = deepcopy({key: value for key, value in raw.items() if key not in LEGACY_CLAUDE_FIELDS})
    if base is None:
        return result
    attachments = [block.model_dump(mode="json") for block in base]
    claude = next((item for item in attachments if item["name"] == "claude-code"), None)
    if claude is None and any(legacy.values()):
        claude = {"name": "claude-code"}
        attachments.append(claude)
    if claude is not None:
        for field, entries in legacy.items():
            claude[field] = list(dict.fromkeys([*claude.get(field, []), *entries]))
    if attachments:
        result["harness_integrations"] = cast("list[JsonValue]", attachments)
    return result


def translated_record(record: DesiredOverlayRecord, base: list[CapabilityBlock] | None) -> DesiredOverlayRecord:
    """Copy a recognized envelope while retaining its unrelated component data."""
    raw = legacy_component(record)
    if raw is None:
        return record
    translated = translate_component(raw, base, agent=record.instance_kind == "agent")
    value = translated if record.instance_kind == "agent" else {**record.payload.value, "admin": translated}
    return replace(record, payload=VersionedPayload(record.payload.payload_version, value))


def checkpoint_conversion(db: Database, original: DesiredOverlayRecord | None, canonical: VersionedPayload) -> None:
    """Commit a successful conversion within the caller's terminal transaction."""
    if original is None or legacy_component(original) is None:
        return
    current = db.instance_state.get_desired_overlay(original.instance_kind, original.instance_name)
    if current != original:
        raise StateError("stored instance spec changed during legacy Claude conversion; retry against current state")
    db.instance_state.put_desired_overlay(original.instance_kind, original.instance_name, canonical)
