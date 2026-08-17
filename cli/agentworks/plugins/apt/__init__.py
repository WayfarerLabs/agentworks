"""Optional apt resource manifests shipped as the ``apt`` system plugin."""

from __future__ import annotations

from agentworks.plugins.base import Plugin

PLUGIN = Plugin(
    name="apt",
    description="Optional apt sources and package sets",
    capabilities={},
    manifests="agentworks.plugins.apt",
)

__all__ = ["PLUGIN"]
