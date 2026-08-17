"""Fixed destinations for the catalog-free guide trail sign."""

from __future__ import annotations

from dataclasses import dataclass

from agentworks.guide.contract import TopicSlug


@dataclass(frozen=True, slots=True)
class TrailDestination:
    slug: TopicSlug
    intent: str


TRAIL_DESTINATIONS: tuple[TrailDestination, ...] = (
    TrailDestination(TopicSlug("concept-assistant-agent"), "Working with an Agentworks assistant agent"),
    TrailDestination(TopicSlug("concept-onboarding"), "First setup or current adoption"),
    TrailDestination(TopicSlug("concept-management"), "Configuration and ongoing operation"),
    TrailDestination(TopicSlug("concept-troubleshooting"), "Diagnosis and recovery"),
    TrailDestination(TopicSlug("concept-release-notes"), "Changes across releases"),
    TrailDestination(TopicSlug("concept-migration"), "Exceptional breaking-input migration"),
    TrailDestination(TopicSlug("concept-secrets"), "Secret handling"),
    TrailDestination(TopicSlug("concept-reporting-bugs"), "Product defect reporting"),
)
