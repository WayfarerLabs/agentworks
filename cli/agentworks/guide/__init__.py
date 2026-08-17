"""Public API for the package-owned Markdown guide."""

from agentworks.guide.agent_mode import GuideMode, select_guide_mode
from agentworks.guide.catalog import GuideCatalog, discover_concept_shells
from agentworks.guide.contract import ConceptShell, GuideContentError, GuideSource, UnknownGuideTopicError
from agentworks.guide.service import GuideResponse, render_guide

__all__ = [
    "ConceptShell",
    "GuideCatalog",
    "GuideContentError",
    "GuideMode",
    "GuideResponse",
    "GuideSource",
    "UnknownGuideTopicError",
    "discover_concept_shells",
    "render_guide",
    "select_guide_mode",
]
