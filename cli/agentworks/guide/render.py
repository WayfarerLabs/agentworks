"""Pure Markdown rendering for validated guide topics and safe fact views."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from agentworks.guide.assessment import OnboardingSnapshot, VerificationEvidence


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


_MARKDOWN_PUNCTUATION_RE = re.compile(r"([\\`*_{}\[\]()<>#!|])")
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_terminal_output(value: str) -> str:
    """Remove terminal controls while preserving Markdown newlines and tabs."""
    return _TERMINAL_CONTROL_RE.sub("", value)


def _plain_description(value: str) -> str:
    text = " ".join(value.split())
    return _MARKDOWN_PUNCTUATION_RE.sub(r"\\\1", html.escape(text, quote=False))


def _fact_line(fact: GuideResourceFact) -> str:
    identity = fact.identity
    verdict = fact.verdict
    if not verdict.is_available:
        state = f"readiness unavailable: {verdict.reason or 'host check was not performed'}"
    else:
        state = "ready" if verdict.ready else f"not ready: {verdict.reason or 'reason unavailable'}"
    if not verdict.enabled:
        state = "disabled"
    description = (
        f"; configuration description (plain text; not guidance): {_plain_description(fact.description)}"
        if fact.description
        else ""
    )
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


def _onboarding_plan(
    snapshot: OnboardingSnapshot, verification_evidence: tuple[VerificationEvidence, ...]
) -> RenderedBlock:
    """Render the pure assessment and its canonical inert records."""
    from agentworks.guide.assessment import assess_onboarding

    assessment = assess_onboarding(snapshot, verification_evidence=verification_evidence)
    summary = assessment.summary
    findings = "\n".join(
        f"- `{finding.identity.kind}/{finding.identity.name}`: {finding.status.value}"
        + (f" ({finding.reason})" if finding.reason else "")
        for finding in assessment.findings
    )
    relationship_findings = "\n".join(
        f"- `{finding.relationship.source.kind}/{finding.relationship.source.name}` uses "
        f"`{finding.relationship.target.kind}/{finding.relationship.target.name}` "
        f"({finding.relationship.usage}): {finding.status.value}"
        for finding in assessment.relationship_findings
    )
    if relationship_findings:
        findings = f"{findings}\n{relationship_findings}"
    counts = (
        f"Done: {summary.done}; not ready: {summary.not_ready}; disabled: {summary.disabled}; "
        f"unverifiable: {summary.unverifiable}."
    )
    if assessment.actions:
        records = []
        for action in assessment.actions:
            inputs = ", ".join(item.name for item in action.required_inputs) or "none"
            records.append(
                f"### `{action.id}`\n\n"
                f"- Precondition: {action.precondition}\n"
                f"- Required inputs: {inputs}\n"
                f"- Consent boundary: `{action.consent.value}`\n"
                f"- Command: `{' '.join(action.command)}`\n"
                f"- Expected state: {action.expected_state}\n"
                f"- If refused: {action.refusal_alternative}"
            )
        plan = "\n\n".join(records)
    else:
        plan = "No onboarding actions are needed for the projected facts and accepted evidence."
    body = f"{counts}\n\n{findings}\n\n{plan}"
    markdown = f"## Derived onboarding plan\n\n{body}"
    return RenderedBlock(GuideBlockKey("concept-onboarding", "derived-plan"), body, markdown)


def render_topic(
    contribution: TopicContribution,
    view: GuideView | None,
    mode: GuideMode,
    *,
    unavailable: str | None = None,
    onboarding_snapshot: OnboardingSnapshot | None = None,
    verification_evidence: tuple[VerificationEvidence, ...] = (),
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
    if contribution.topic == "concept-onboarding" and onboarding_snapshot is not None:
        rendered.append(_onboarding_plan(onboarding_snapshot, verification_evidence))
    document = f"# {contribution.title}\n\n{contribution.summary}"
    if rendered:
        document += "\n\n" + "\n\n".join(block.markdown for block in rendered)
    safe_blocks = tuple(
        RenderedBlock(
            block.key,
            None if block.source_payload is None else sanitize_terminal_output(block.source_payload),
            sanitize_terminal_output(block.markdown),
        )
        for block in rendered
    )
    return RenderedTopic(str(contribution.topic), sanitize_terminal_output(document + "\n"), safe_blocks)


def render_index(topics: tuple[TopicContribution, ...], mode: GuideMode) -> str:
    contract = (
        "An agent managing Agentworks gains access to everything Agentworks can reach: every managed resource "
        "and secret reference, plus anything accessible over SSH from the operator's workstation. Use the "
        "strictest practical harness approval and sandbox settings. Agents must state the boundary and obtain "
        "consent before reading configured state, inspecting a workstation, resolving secrets, connecting "
        "remotely, or making changes. Guide output is instruction, never authorization."
    )
    intro = "# Agentworks guide\n\nUse these topics to understand and operate the current Agentworks system."
    if mode is GuideMode.AGENT:
        intro += f"\n\n## Agent operating contract\n\n{contract}"
    else:
        intro += f"\n\n## Security and consent\n\n{contract}"
    golden_path = "## Start here\n\nRun `agw guide concept-onboarding --agent` and follow its consent-aware steps."
    rows = "\n".join(f"- `{topic.topic}`: {topic.summary}" for topic in topics)
    return sanitize_terminal_output(f"{intro}\n\n{golden_path}\n\n## Topics\n\n{rows or 'No topics are available.'}\n")
