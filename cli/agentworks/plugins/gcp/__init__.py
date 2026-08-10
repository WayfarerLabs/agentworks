"""The opt-in ``gcp`` system plugin and its Compute Engine platform."""

from __future__ import annotations

from agentworks.plugins.base import Plugin
from agentworks.plugins.gcp.platform import GCEPlatform

PLUGIN = Plugin(
    name="gcp",
    description="Google Compute Engine VM platform",
    capabilities={"vm-platform": (GCEPlatform,)},
)
