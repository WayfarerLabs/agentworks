"""Backward-compatible env-var reads during the AW_ prefix migration.

Reads the preferred AW_-prefixed name first; falls back to the legacy
name with a one-time deprecation warning per process per legacy name.
"""

from __future__ import annotations

import os

_warned: set[str] = set()


def read_env_with_legacy(new_name: str, legacy_name: str) -> str | None:
    """Read an env var, preferring the new AW_-prefixed name.

    Falls back to ``legacy_name``. When the legacy name is the source,
    emits a one-time deprecation warning per process for that legacy name.
    """
    value = os.environ.get(new_name)
    if value is not None:
        return value
    legacy_value = os.environ.get(legacy_name)
    if legacy_value is not None and legacy_name not in _warned:
        _warned.add(legacy_name)
        from agentworks import output

        output.warn(
            f"Environment variable {legacy_name!r} is deprecated; "
            f"set {new_name!r} instead.",
        )
    return legacy_value


def reset_warning_cache() -> None:
    """Test-only: clear the per-process warning cache."""
    _warned.clear()
