"""Backward-compatible env-var reads during the AW_ prefix migration.

Reads the preferred AW_-prefixed name first; falls back to the legacy
name with a one-time deprecation warning per process per legacy name.
When both are set, the new name wins and a one-time informational note
fires to nudge the operator to unset the now-redundant legacy var.
"""

from __future__ import annotations

import os

from agentworks import output

_warned: set[str] = set()


def read_env_with_legacy(new_name: str, legacy_name: str) -> str | None:
    """Read an env var, preferring the new AW_-prefixed name.

    Falls back to ``legacy_name``. When the legacy name is the source,
    emits a one-time deprecation warning per process for that legacy name.
    When both names are set, the new name wins and a one-time
    informational note per process per legacy name tells the operator the
    legacy var is sitting unused in their shell.
    """
    value = os.environ.get(new_name)
    if value is not None:
        legacy_value = os.environ.get(legacy_name)
        if legacy_value is not None:
            key = f"both:{legacy_name}"
            if key not in _warned:
                _warned.add(key)
                output.warn(
                    f"Environment variable {legacy_name!r} is set but ignored; "
                    f"{new_name!r} takes precedence. Unset {legacy_name!r} to silence this.",
                )
        return value
    legacy_value = os.environ.get(legacy_name)
    if legacy_value is not None:
        key = f"legacy:{legacy_name}"
        if key not in _warned:
            _warned.add(key)
            output.warn(
                f"Environment variable {legacy_name!r} is deprecated and will be "
                f"removed in a future release; set {new_name!r} instead.",
            )
    return legacy_value


def reset_warning_cache() -> None:
    """Test-only: clear the per-process warning cache."""
    _warned.clear()
