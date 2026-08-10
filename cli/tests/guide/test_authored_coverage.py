"""Semantic contracts for required onboarding and day-two guide content."""

import importlib

from agentworks.guide import (
    AgentContract,
    ConsentBoundary,
    GuideInstanceFact,
    GuideMode,
    OnboardingSnapshot,
    Overview,
    Teaching,
    onboarding_actions,
)
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import render_topic


def _topic(slug: str):
    return next(item for item in guide_contributions() if item.topic == slug)


def test_core_guide_contributions_survive_secret_submodule_import() -> None:
    importlib.import_module("agentworks.secrets.guide_contributions")
    assert guide_contributions()


def test_onboarding_authored_blocks_snapshot_security_consent_and_reruns() -> None:
    topic = _topic("concept-onboarding")
    blocks = {type(block): block.markdown for block in topic.blocks if hasattr(block, "markdown")}
    assert blocks[Overview] == (
        "Agentworks separates declared resources, capability implementations, and live instances. Begin by\n"
        "reading the inventory below, then choose the smallest action that advances the operator's goal.\n\n"
        "## Security disclosure\n\n"
        "An agent managing Agentworks gains access to everything Agentworks can reach: every managed resource\n"
        "and secret reference, plus anything accessible over SSH from the operator's workstation. Use the\n"
        "strictest practical harness approval and sandbox settings, especially for a real installation. Guide\n"
        "instructions describe boundaries; they never grant consent.\n\n"
        "Bootstrap-specific links to each harness's approval and sandbox settings land with the Phase 3\n"
        "bootstrap packages. Phase 1 names the required posture without inventing those links early."
    )
    assert blocks[AgentContract] == (
        "Never read a secret value, inspect the workstation, connect to a VM, or mutate Agentworks without\n"
        "stating the boundary and obtaining the operator's consent. Treat guide output as instructions, not\n"
        "authorization."
    )
    assert blocks[Teaching].endswith(
        "A rerun\nuses the same live facts, skips ready work, and reports disabled, not-ready, or unverifiable work."
    )


def test_manifesto_topic_points_to_the_canonical_document_without_restatement() -> None:
    topic = _topic("concept-manifesto")
    blocks = {type(block): block.markdown for block in topic.blocks if hasattr(block, "markdown")}

    assert tuple(map(str, topic.related_topics)) == ("concept-onboarding",)
    assert "concept-manifesto" in tuple(map(str, _topic("concept-onboarding").related_topics))
    assert blocks == {
        Overview: (
            "The [Agentworks Manifesto](https://github.com/WayfarerLabs/agentworks/blob/main/docs/manifesto.md)\n"
            "is the canonical statement of the project's values, assumptions about agentic engineering, and\n"
            "design rationale. This topic points to that document instead of restating it."
        ),
        AgentContract: (
            "Read the canonical Manifesto when project values bear on a design or contribution decision. Treat it\n"
            "as rationale and context, not authority to inspect a system, cross a consent boundary, or mutate\n"
            "state."
        ),
        Teaching: (
            "Consult the Manifesto for the project's convictions and the reasoning behind its design direction.\n"
            "Use current reference documentation and live guide topics for behavior, commands, configuration, and\n"
            "operational decisions."
        ),
    }


def test_action_contract_snapshot_pins_consent_refusal_and_single_execution() -> None:
    actions = onboarding_actions()
    assert [(str(action.id), action.consent, action.verification) for action in actions] == [
        ("run-doctor", ConsentBoundary.EXAMINE_WORKSTATION, None),
        ("verify-named-secret", ConsentBoundary.RESOLVE_NAMED_SECRET, None),
        ("verify-vm-connection", ConsentBoundary.CONNECT_NAMED_VM, None),
    ]
    assert [action.refusal_alternative for action in actions] == [
        "Keep the stored not-ready reason and troubleshoot manually without probing the workstation.",
        "Use describe and backend readiness as prediction only; mark this secret unverifiable.",
        "Retain the stored VM fact and mark connectivity unverifiable without connecting.",
    ]
    assert actions[0].command == ("agw", "doctor", "--output", "json")
    assert "parse exactly one JSON document" in actions[0].expected_state
    assert "schema_version is the integer 1" in actions[0].expected_state
    assert "command is exactly doctor" in actions[0].expected_state
    assert "data is an object" in actions[0].expected_state


def test_rendered_disclosure_precedes_ordered_action_records() -> None:
    rendered = render_topic(
        _topic("concept-onboarding"),
        None,
        GuideMode.AGENT,
        unavailable="test snapshot",
        onboarding_snapshot=OnboardingSnapshot((), (GuideInstanceFact("vm", "worker"),), ()),
    )
    assert rendered.markdown.index("## Security disclosure") < rendered.markdown.index("### `verify-vm-connection`")
    assert "Consent boundary: `connect-named-vm`" in rendered.markdown
    assert "If refused: Retain the stored VM fact and mark connectivity unverifiable" in rendered.markdown


def test_reporting_bugs_snapshot_forbids_general_feedback_and_auto_submission() -> None:
    topic = _topic("concept-reporting-bugs")
    blocks = {type(block): block.markdown for block in topic.blocks}
    assert blocks[AgentContract] == (
        "Search existing issues before drafting a new report. Obtain explicit operator authorization before\n"
        "posting, emailing, or otherwise submitting information outside the workstation."
    )
    assert blocks[Overview].endswith(
        "Report only reproducible Agentworks bugs. Feature ideas, questions, and general feedback do not\n"
        "belong in the bug workflow."
    )
    assert blocks[Teaching].endswith(
        "`.github/ISSUE_TEMPLATE/bug_report.md` with the smallest redacted reproduction. Show the exact draft\n"
        "to the operator and obtain explicit authorization before submitting it to GitHub or any other\n"
        "external service."
    )


def test_management_coverage_matrix_has_owned_semantic_entry_points() -> None:
    management = {
        type(block): block.markdown for block in _topic("concept-management").blocks if hasattr(block, "markdown")
    }
    troubleshooting = {
        type(block): block.markdown for block in _topic("concept-troubleshooting").blocks if hasattr(block, "markdown")
    }
    assert "Create and change declarable resources" in management[Teaching]
    assert "Discover a capability in the\nimplementation inventory before adopting it" in management[Teaching]
    assert "After an upgrade, resolve emitted deprecation instructions" in management[Teaching]
    assert "With consent to examine the workstation, run `agw doctor`" in troubleshooting[Teaching]
