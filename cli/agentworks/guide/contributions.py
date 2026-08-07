"""Trusted, authored guide contributions shipped with Agentworks."""

from __future__ import annotations

from importlib.resources import files

from agentworks.guide.contract import (
    AgentContract,
    BlockId,
    ConceptAnchor,
    GuideBlock,
    InstanceList,
    Overview,
    Teaching,
    TopicContribution,
    TopicSlug,
)


def _markdown(topic: str, block_id: str) -> str:
    resource = files("agentworks.guide").joinpath("guide-content", topic, f"{block_id}.md")
    return resource.read_text(encoding="utf-8").strip()


def _concept(
    slug: str,
    title: str,
    summary: str,
    *,
    inventory: bool = False,
) -> TopicContribution:
    blocks: tuple[GuideBlock, ...] = (
        Overview(BlockId("overview"), _markdown(slug, "overview")),
        AgentContract(BlockId("agent-contract"), _markdown(slug, "agent-contract")),
        Teaching(BlockId("teaching"), _markdown(slug, "teaching")),
    )
    if inventory:
        blocks += (InstanceList(BlockId("inventory")),)
    return TopicContribution(TopicSlug(slug), title, summary, ConceptAnchor(slug), blocks)


def guide_contributions() -> tuple[TopicContribution, ...]:
    """Load core prose only when a guide request builds its catalog."""
    from agentworks.secrets import guide_contributions as secret_guide_contributions

    return (
        _concept(
            "concept-onboarding",
            "Agentworks onboarding",
            "Start safely, inspect the current system, and take one consented step at a time.",
            inventory=True,
        ),
        _concept(
            "concept-management",
            "Resource management",
            "Use declared resources, capability implementations, and live instances deliberately.",
        ),
        _concept(
            "concept-troubleshooting",
            "Troubleshooting",
            "Diagnose from framed errors and explicit checks before attempting repairs.",
        ),
        _concept(
            "concept-reporting-bugs",
            "Reporting bugs",
            "Prepare a redacted reproduction and obtain approval before submitting it externally.",
        ),
    ) + secret_guide_contributions()
