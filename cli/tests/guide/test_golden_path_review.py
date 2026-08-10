from __future__ import annotations

from agentworks.guide import (
    ActionId,
    GuideIdentity,
    GuideMode,
    GuideOrigin,
    GuideResourceFact,
    GuideVerdict,
    OnboardingSnapshot,
    OnboardingStatus,
    assess_onboarding,
)
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import render_index, render_topic
from agentworks.guide.service import _dynamic_topic
from agentworks.guide.view import build_guide_view
from agentworks.resources.graph import Enablement, Readiness


def test_unavailable_readiness_renders_and_assesses_as_unverifiable() -> None:
    reason = "host readiness unavailable: guide does not inspect the workstation"
    fact = GuideResourceFact(
        GuideIdentity("vm-site", "lima-local"),
        "declarable",
        None,
        GuideOrigin("built-in", None),
        GuideVerdict(enabled=True, ready=False, is_available=False, reason=reason),
    )

    assessment = assess_onboarding(OnboardingSnapshot((fact,), (), ()))

    assert assessment.findings[0].status is OnboardingStatus.UNVERIFIABLE
    assert assessment.findings[0].reason == reason
    assert ActionId("run-doctor") not in assessment.action_ids
    assert assessment.action_ids[-2:] == (ActionId("create-first-vm"), ActionId("create-first-session"))


def test_unavailable_readiness_is_explicit_in_dynamic_rendering() -> None:
    class Graph:
        def readiness_of(self, kind: str, name: str) -> Readiness:
            return Readiness.unavailable("guide does not inspect the workstation")

        def enablement_of(self, kind: str, name: str) -> Enablement:
            return Enablement.enabled

        def edges_of(self, kind: str, name: str) -> tuple[()]:
            return ()

        def dependents_of(self, kind: str, name: str) -> tuple[()]:
            return ()

    class Registry:
        is_finalized = True
        graph = Graph()
        resource = type("Resource", (), {"description": None, "origin": None})()

        def lookup(self, kind: str, name: str) -> object:
            return self.resource

        def iter_kind_items(self, kind: str):
            return iter((("lima", self.resource),)) if kind == "vm-platform" else iter(())

    registry = Registry()
    topic = _dynamic_topic(registry, "vm-platform/lima")  # type: ignore[arg-type]
    view = build_guide_view(topic, registry, object())  # type: ignore[arg-type]
    markdown = render_topic(topic, view, GuideMode.AGENT).markdown

    assert "readiness unavailable: guide does not inspect the workstation" in markdown
    assert "not ready:" not in markdown


def test_no_topic_modes_have_semantic_parity_and_complete_intent_map() -> None:
    topics = guide_contributions()
    human = render_index(topics, GuideMode.HUMAN)
    agent = render_index(topics, GuideMode.AGENT)

    disclosure = "The Agentworks assistant agent runs on the intended workstation"
    for markdown in (human, agent):
        assert all(line.startswith("## ⟦AGW framework⟧") for line in markdown.splitlines() if line.startswith("## "))
        disclosure_at = markdown.index(disclosure)
        intent_at = markdown.index("## ⟦AGW framework⟧ Intent map")
        topics_at = markdown.index("## ⟦AGW framework⟧ Topics")
        assert disclosure_at < intent_at < topics_at
        for destination in (
            "concept-onboarding",
            "concept-release-notes",
            "concept-management",
            "concept-troubleshooting",
            "concept-migration",
            "concept-secrets",
            "concept-reporting-bugs",
        ):
            assert f"`{destination}`" in markdown[intent_at:topics_at]
        assert "decides what topic, proposal, or inert action to use next" in markdown
        assert "does not route the request or grant authority" in markdown

    human_semantics = human.replace("## ⟦AGW framework⟧ Security and consent", "## ⟦AGW framework⟧ Disclosure")
    agent_semantics = agent.replace("## ⟦AGW framework⟧ Agent operating contract", "## ⟦AGW framework⟧ Disclosure")
    assert human_semantics == agent_semantics
