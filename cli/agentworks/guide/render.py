"""Pure Markdown rendering for validated guide topics and safe fact views."""

from __future__ import annotations

from dataclasses import dataclass

from agentworks.guide.agent_mode import GuideMode
from agentworks.guide.contract import (
    AgentContract,
    FieldReference,
    GuideBlock,
    GuideTraversalError,
    InstanceList,
    Overview,
    Relationships,
    Sample,
    State,
    Teaching,
    TopicContribution,
    TopicLinks,
)
from agentworks.guide.view import GuideResourceFact, GuideRoot, GuideView


@dataclass(frozen=True, slots=True)
class GuideBlockKey:
    topic: str
    block_id: str


@dataclass(frozen=True, slots=True)
class RenderedBlock:
    key: GuideBlockKey
    source_payload: str | None
    markdown: str


@dataclass(frozen=True, slots=True)
class RenderedTopic:
    topic: str
    markdown: str
    blocks: tuple[RenderedBlock, ...]


def _fact_line(fact: GuideResourceFact) -> str:
    identity = fact.identity
    verdict = fact.verdict
    state = "ready" if verdict.ready else f"not ready: {verdict.reason or 'reason unavailable'}"
    if not verdict.enabled:
        state = "disabled"
    description = f": {fact.description}" if fact.description else ""
    return f"- `{identity.kind}/{identity.name}` ({state}){description}"


def _dynamic(block: GuideBlock, view: GuideView) -> str:
    if isinstance(block, InstanceList):
        try:
            instances = view.instances()
            if instances:
                return "\n".join(f"- `{item.kind}/{item.name}`" for item in instances)
        except GuideTraversalError:  # traversal is deliberately retried through permitted concept roots
            pass
        facts: tuple[GuideResourceFact, ...] = ()
        for root in (GuideRoot.KINDS, GuideRoot.IMPLEMENTATIONS):
            try:
                facts += view.inventory(root)
            except GuideTraversalError:
                continue
        return "\n".join(_fact_line(fact) for fact in facts) or "No current items."
    if isinstance(block, State):
        return _fact_line(view.me())
    if isinstance(block, Relationships):
        lines = [
            *(f"- Uses `{item.target.kind}/{item.target.name}`: {item.usage}" for item in view.outbound()),
            *(f"- Used by `{item.source.kind}/{item.source.name}`: {item.usage}" for item in view.inbound()),
        ]
        return "\n".join(lines) or "No current relationships."
    if isinstance(block, (FieldReference, Sample)):
        return "This section is available after schema services are installed."
    if isinstance(block, TopicLinks):
        return ""
    raise TypeError(f"unsupported dynamic guide block {type(block).__name__}")


def _heading(block: GuideBlock, mode: GuideMode) -> str:
    if isinstance(block, Overview):
        return "Overview"
    if isinstance(block, Teaching):
        return "How it works"
    if isinstance(block, AgentContract):
        return "Agent operating contract" if mode is GuideMode.AGENT else "Consent and safety"
    if isinstance(block, InstanceList):
        return "Current inventory"
    if isinstance(block, State):
        return "Current state"
    if isinstance(block, Relationships):
        return "Relationships"
    if isinstance(block, FieldReference):
        return "Fields"
    if isinstance(block, Sample):
        return "Sample"
    return "Related topics"


def render_topic(
    contribution: TopicContribution,
    view: GuideView | None,
    mode: GuideMode,
    *,
    unavailable: str | None = None,
) -> RenderedTopic:
    """Render one topic without consulting configuration or invoking capabilities."""
    blocks = contribution.blocks
    if mode is GuideMode.AGENT:
        blocks = tuple(block for block in blocks if isinstance(block, AgentContract)) + tuple(
            block for block in blocks if not isinstance(block, AgentContract)
        )
    rendered: list[RenderedBlock] = []
    for block in blocks:
        source = block.markdown if isinstance(block, (Overview, Teaching, AgentContract)) else None
        if source is not None:
            body = source
        elif unavailable is not None:
            body = f"Live facts unavailable: {unavailable}"
        elif isinstance(block, TopicLinks):
            body = "\n".join(f"- `{topic}`" for topic in contribution.related_topics) or "No related topics."
        else:
            if view is None:
                raise ValueError("dynamic guide blocks require a view or unavailable reason")
            body = _dynamic(block, view)
        if source is None:
            source = body
        markdown = f"## {_heading(block, mode)}\n\n{body}"
        rendered.append(RenderedBlock(GuideBlockKey(str(contribution.topic), str(block.id)), source, markdown))
    document = f"# {contribution.title}\n\n{contribution.summary}"
    if rendered:
        document += "\n\n" + "\n\n".join(block.markdown for block in rendered)
    return RenderedTopic(str(contribution.topic), document + "\n", tuple(rendered))


def render_index(topics: tuple[TopicContribution, ...], mode: GuideMode) -> str:
    contract = (
        "Agents must obtain consent before reading configured state, inspecting a workstation, "
        "resolving secrets, connecting remotely, or making changes."
    )
    intro = "# Agentworks guide\n\nUse these topics to understand and operate the current Agentworks system."
    if mode is GuideMode.AGENT:
        intro += f"\n\n## Agent operating contract\n\n{contract}"
    else:
        intro += f"\n\n## Security and consent\n\n{contract}"
    rows = "\n".join(f"- `{topic.topic}`: {topic.summary}" for topic in topics)
    return f"{intro}\n\n## Topics\n\n{rows or 'No topics are available.'}\n"
