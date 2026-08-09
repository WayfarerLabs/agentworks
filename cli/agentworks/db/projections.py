"""Closed output projections for values read from persisted database rows."""

from __future__ import annotations

from enum import Enum, StrEnum

UNKNOWN_PERSISTED_VALUE = "unknown"
"""Stable JSON v1 sentinel for an invalid persisted closed-enum value."""


class SessionReportStatus(StrEnum):
    """Closed JSON v1 vocabulary for reported session liveness."""

    RUNNING = "running"
    STOPPED = "stopped"
    BROKEN = "broken"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


def project_persisted_enum(value: object, enum_type: type[Enum]) -> str:
    """Map one persisted enum value to its closed value or ``unknown``."""
    if type(value) is not str:
        return UNKNOWN_PERSISTED_VALUE
    try:
        projected = enum_type(value).value
    except (TypeError, ValueError):
        return UNKNOWN_PERSISTED_VALUE
    return projected if type(projected) is str else UNKNOWN_PERSISTED_VALUE


def project_session_status(value: object, *, allow_unavailable: bool) -> str:
    """Close session status, reserving unavailable for skipped live work."""
    projected = project_persisted_enum(value, SessionReportStatus)
    if projected == SessionReportStatus.UNAVAILABLE and not allow_unavailable:
        return SessionReportStatus.UNKNOWN
    return projected
