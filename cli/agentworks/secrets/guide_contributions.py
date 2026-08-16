"""Authored guide content owned by the secrets subsystem."""

from __future__ import annotations

from importlib.resources import files

from agentworks.guide.contract import (
    AgentContract,
    BlockId,
    Overview,
    Teaching,
    TopicContribution,
    TopicSlug,
)


def _markdown(block_id: str) -> str:
    resource = files("agentworks.secrets").joinpath("guide-content", "concept-secrets", f"{block_id}.md")
    return resource.read_text(encoding="utf-8").strip()


def guide_contributions() -> tuple[TopicContribution, ...]:
    """Return inert secrets teaching loaded from package resources."""
    return (
        TopicContribution(
            TopicSlug("concept-secrets"),
            "Secrets",
            "Understand secret declarations, sources, and typed proof without exposing secret values.",
            (
                Overview(BlockId("overview"), _markdown("overview")),
                AgentContract(BlockId("agent-contract"), _markdown("agent-contract")),
                Teaching(BlockId("teaching"), _markdown("teaching")),
            ),
        ),
    )
