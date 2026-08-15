"""Optional user install-command manifests shipped as a system plugin."""

from __future__ import annotations

from agentworks.plugins.base import Plugin

PLUGIN = Plugin(
    name="install-command",
    description="Optional user install commands",
    capabilities={},
    manifests="agentworks.plugins.install_command",
)
