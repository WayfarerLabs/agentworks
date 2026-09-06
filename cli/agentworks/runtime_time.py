"""Shared start-time projection and human duration formatting."""

from __future__ import annotations

from datetime import UTC, datetime

from agentworks.errors import StateError

_STORED_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def derive_uptime_seconds(
    last_started_at: str | None,
    *,
    running: bool,
    entity_kind: str,
    entity_name: str,
    now: datetime | None = None,
) -> int | None:
    """Validate a persisted start time and derive current whole-second uptime."""
    if last_started_at is None:
        return None
    try:
        started_at = datetime.strptime(last_started_at, _STORED_UTC_FORMAT).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise StateError(
            f"stored {entity_kind} last start time is malformed",
            entity_kind=entity_kind,
            entity_name=entity_name,
            hint="Restore a valid Agentworks database backup.",
        ) from None
    if not running:
        return None
    observed_at = datetime.now(UTC) if now is None else now
    return max(0, int((observed_at - started_at).total_seconds()))


def format_duration(seconds: int | None) -> str:
    """Format a nonnegative duration compactly, or report unknown."""
    if seconds is None:
        return "unknown"
    if seconds < 0:
        raise ValueError("duration must be nonnegative")
    remainder = seconds
    parts: list[str] = []
    for unit_seconds, suffix in ((86_400, "d"), (3_600, "h"), (60, "m")):
        value, remainder = divmod(remainder, unit_seconds)
        if value:
            parts.append(f"{value}{suffix}")
    if remainder or not parts:
        parts.append(f"{remainder}s")
    return " ".join(parts)
