"""Installed Agentworks CLI version authority."""

from __future__ import annotations

_DIST_NAME = "agentworks-cli"


def resolve_version() -> str:
    """Return the installed distribution version or ``unknown`` in a source tree."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(_DIST_NAME)
    except PackageNotFoundError:
        return "unknown"
