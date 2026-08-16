"""Semantic contracts for required onboarding and day-two guide content."""

import importlib

from agentworks.guide import (
    ActionList,
    AgentContract,
    ConsentBoundary,
    Overview,
    ReleaseNotes,
    Teaching,
    TopicLinks,
    onboarding_actions,
)
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.service import build_authored_catalog


def _topic(slug: str):
    return next(item for item in guide_contributions() if item.topic == slug)


def test_core_guide_contributions_survive_secret_submodule_import() -> None:
    importlib.import_module("agentworks.secrets.guide_contributions")
    assert guide_contributions()


def test_manifesto_topic_is_structurally_linked_to_onboarding() -> None:
    topic = _topic("concept-manifesto")

    assert tuple(map(str, topic.related_topics)) == ("concept-onboarding",)
    assert "concept-manifesto" in tuple(map(str, _topic("concept-onboarding").related_topics))
    assert {type(block) for block in topic.blocks} == {Overview, AgentContract, Teaching, TopicLinks}


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


def test_catalog_contains_only_retained_authored_block_shapes() -> None:
    catalog = build_authored_catalog(strict=True)
    retained_types = {Overview, Teaching, AgentContract, ReleaseNotes, ActionList, TopicLinks}

    assert {type(block) for topic in catalog.topics for block in topic.blocks} <= retained_types
    assert all(str(block.id) != "inventory" for topic in catalog.topics for block in topic.blocks)
