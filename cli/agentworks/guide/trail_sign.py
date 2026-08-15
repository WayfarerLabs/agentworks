"""Fixed destinations for the catalog-free guide trail sign."""

from __future__ import annotations

from dataclasses import dataclass

from agentworks.guide.agent_mode import GuideMode
from agentworks.guide.contract import TopicSlug


@dataclass(frozen=True, slots=True)
class TrailDestination:
    slug: TopicSlug
    agent_intent: str
    human_choice: str | None = None


TRAIL_DESTINATIONS = (
    TrailDestination(TopicSlug("concept-onboarding"), "First setup or current adoption", "New installation"),
    TrailDestination(
        TopicSlug("concept-management"),
        "Configuration and ongoing operation",
        "Existing installation",
    ),
    TrailDestination(TopicSlug("concept-troubleshooting"), "Diagnosis and recovery"),
    TrailDestination(TopicSlug("concept-release-notes"), "Changes across releases"),
    TrailDestination(TopicSlug("concept-migration"), "Exceptional breaking-input migration"),
    TrailDestination(TopicSlug("concept-secrets"), "Secret handling"),
    TrailDestination(TopicSlug("concept-reporting-bugs"), "Product defect reporting"),
)


def trail_destinations(mode: GuideMode) -> tuple[TrailDestination, ...]:
    """Return the fixed destinations visible in one presentation mode."""
    if mode is GuideMode.AGENT:
        return TRAIL_DESTINATIONS
    return tuple(destination for destination in TRAIL_DESTINATIONS if destination.human_choice is not None)
