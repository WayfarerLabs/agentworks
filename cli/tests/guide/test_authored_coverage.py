"""Semantic contracts for required onboarding and day-two guide content."""

import importlib

from agentworks.guide import (
    AgentContract,
    ConsentBoundary,
    InstanceList,
    Overview,
    Teaching,
    onboarding_actions,
)
from agentworks.guide.contributions import guide_contributions


def _topic(slug: str):
    return next(item for item in guide_contributions() if item.topic == slug)


def test_core_guide_contributions_survive_secret_submodule_import() -> None:
    importlib.import_module("agentworks.secrets.guide_contributions")
    assert guide_contributions()


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


def test_action_contract_pins_boundaries_commands_and_verification() -> None:
    actions = onboarding_actions()
    assert [(str(action.id), action.consent, action.verification) for action in actions] == [
        ("run-doctor", ConsentBoundary.EXAMINE_WORKSTATION, None),
        ("verify-named-secret", ConsentBoundary.RESOLVE_NAMED_SECRET, None),
        ("verify-vm-connection", ConsentBoundary.CONNECT_NAMED_VM, None),
        (
            "create-first-vm",
            ConsentBoundary.MUTATE_AGENTWORKS,
            ("agw", "vm", "describe", "$VM_NAME", "--output", "json"),
        ),
        (
            "create-first-session",
            ConsentBoundary.MUTATE_AGENTWORKS,
            ("agw", "session", "describe", "$SESSION_NAME", "--output", "json"),
        ),
    ]
    assert all(action.refusal_alternative for action in actions)
    assert actions[0].command == ("agw", "doctor", "--output", "json")
    assert actions[3].command == (
        "agw",
        "vm",
        "create",
        "$VM_NAME",
        "--template",
        "$VM_TEMPLATE",
        "--admin-template",
        "$ADMIN_TEMPLATE",
        "--site",
        "$VM_SITE",
    )
    assert actions[4].command == (
        "agw",
        "session",
        "create",
        "$SESSION_NAME",
        "--template",
        "$SESSION_TEMPLATE",
        "--vm",
        "$VM_NAME",
        "--new-workspace",
        "--workspace-name",
        "$WORKSPACE_NAME",
        "--workspace-template",
        "$WORKSPACE_TEMPLATE",
        "--new-agent",
        "--agent-name",
        "$AGENT_NAME",
        "--agent-template",
        "$AGENT_TEMPLATE",
    )


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


def test_management_topic_includes_live_instances() -> None:
    assert any(isinstance(block, InstanceList) for block in _topic("concept-management").blocks)
