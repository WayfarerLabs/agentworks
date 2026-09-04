"""Closed output projections for values read from persisted database rows."""

from __future__ import annotations

from enum import Enum, StrEnum

UNKNOWN_PERSISTED_VALUE = "unknown"
"""Stable JSON v1 sentinel for an invalid persisted closed-enum value."""


class VMProvisioningOutputStatus(StrEnum):
    """Frozen JSON v1 vocabulary for VM provisioning state."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"
    UNKNOWN = "unknown"


class VMInitializationOutputStatus(StrEnum):
    """Frozen JSON v1 vocabulary for VM initialization state."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class SessionOutputMode(StrEnum):
    """Frozen JSON v1 vocabulary for persisted session mode."""

    ADMIN = "admin"
    AGENT = "agent"
    UNKNOWN = "unknown"


class SessionReportStatus(StrEnum):
    """Closed JSON v1 vocabulary for reported session liveness."""

    RUNNING = "running"
    STOPPED = "stopped"
    BROKEN = "broken"
    RESIDUAL = "residual"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


def _project_output_enum(value: object, enum_type: type[Enum]) -> str:
    """Map one value through an output-owned vocabulary or ``unknown``."""
    if type(value) is not str:
        return UNKNOWN_PERSISTED_VALUE
    try:
        projected = enum_type(value).value
    except (TypeError, ValueError):
        return UNKNOWN_PERSISTED_VALUE
    return projected if type(projected) is str else UNKNOWN_PERSISTED_VALUE


def project_vm_provisioning_status(value: object) -> str:
    """Close persisted provisioning state to the frozen JSON v1 vocabulary."""
    return _project_output_enum(value, VMProvisioningOutputStatus)


def project_vm_initialization_status(value: object) -> str:
    """Close persisted initialization state to the frozen JSON v1 vocabulary."""
    return _project_output_enum(value, VMInitializationOutputStatus)


def project_session_mode(value: object) -> str:
    """Close persisted session mode to the frozen JSON v1 vocabulary."""
    return _project_output_enum(value, SessionOutputMode)


def project_session_status(value: object, *, allow_unavailable: bool) -> str:
    """Close session status, reserving unavailable for skipped live work."""
    projected = _project_output_enum(value, SessionReportStatus)
    if projected == SessionReportStatus.UNAVAILABLE and not allow_unavailable:
        return SessionReportStatus.UNKNOWN
    return projected
