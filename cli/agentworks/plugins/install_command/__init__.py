"""Optional user install-command manifests shipped as a system plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.plugins.base import Plugin

if TYPE_CHECKING:
    from agentworks.guide.contract import TopicContribution


def _load_guide_contributions() -> tuple[TopicContribution, ...]:
    """Load install-command teaching only while a guide request builds its catalog."""
    from agentworks.plugins.install_command.guide_contributions import guide_contributions as load_contributions

    return load_contributions()


PLUGIN = Plugin(
    name="install-command",
    description="Optional user install commands",
    capabilities={},
    manifests="agentworks.plugins.install_command",
)

__all__ = ["PLUGIN"]
