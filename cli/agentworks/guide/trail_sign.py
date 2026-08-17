"""Fixed destinations and rendering for the catalog-free guide trail sign."""

from __future__ import annotations

from dataclasses import dataclass

from agentworks.guide.agent_mode import GuideMode
from agentworks.terminal import sanitize_terminal_output


@dataclass(frozen=True, slots=True)
class TrailDestination:
    slug: str
    intent: str


TRAIL_DESTINATIONS: tuple[TrailDestination, ...] = (
    TrailDestination("concept-assistant-agent", "Working with an Agentworks assistant agent"),
    TrailDestination("concept-onboarding", "First setup or current adoption"),
    TrailDestination("concept-management", "Configuration and ongoing operation"),
    TrailDestination("concept-troubleshooting", "Diagnosis and recovery"),
    TrailDestination("concept-release-notes", "Changes across releases"),
    TrailDestination("concept-migration", "Exceptional breaking-input migration"),
    TrailDestination("concept-secrets", "Secret handling"),
    TrailDestination("concept-reporting-bugs", "Product defect reporting"),
)


def render_trail_sign(mode: GuideMode) -> str:
    """Render the fixed destination sign without consulting the topic catalog."""
    rows = "\n".join(f"- {destination.intent}: `{destination.slug}`." for destination in TRAIL_DESTINATIONS)
    if mode is GuideMode.AGENT:
        intro = "Start with `concept-assistant-agent`, then choose the destination for the operator's goal."
    else:
        intro = "Choose the destination for your goal."
    return sanitize_terminal_output(
        f"# Agentworks guide\n\n{intro}\n\n## Destinations\n\n{rows}\n\n"
        "Use shell completion or `agw guide --names-only` to discover every installed topic.\n"
    )
