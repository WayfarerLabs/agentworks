"""Optional apt resource manifests shipped as the ``apt`` system plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.plugins.base import Plugin

if TYPE_CHECKING:
    from agentworks.guide.contract import TopicContribution


def _load_guide_contributions() -> tuple[TopicContribution, ...]:
    """Load apt teaching only while a guide request builds its catalog."""
    from agentworks.plugins.apt.guide_contributions import guide_contributions as load_contributions

    globals()["guide_contributions"] = _load_guide_contributions
    return load_contributions()


guide_contributions = _load_guide_contributions

PLUGIN = Plugin(
    name="apt",
    description="Optional apt sources and package sets",
    capabilities={},
    manifests="agentworks.plugins.apt",
)

__all__ = ["PLUGIN", "guide_contributions"]
