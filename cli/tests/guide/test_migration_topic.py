from __future__ import annotations

from agentworks.guide import ActionList, ConsentBoundary, GuideMode, TopicLinks
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import render_topic


def _topic(slug: str):
    return next(topic for topic in guide_contributions() if topic.topic == slug)


def test_migration_is_a_colocated_exception_topic_linked_without_teaching_duplication() -> None:
    migration = _topic("concept-migration")
    migration_teaching = next(block.markdown for block in migration.blocks if hasattr(block, "markdown"))
    for slug in ("concept-onboarding", "concept-management"):
        topic = _topic(slug)
        assert "concept-migration" in topic.related_topics
        assert any(isinstance(block, TopicLinks) for block in topic.blocks)
        assert migration_teaching not in "\n".join(
            block.markdown for block in topic.blocks if hasattr(block, "markdown")
        )
    assert "general upgrade" in migration.blocks[0].markdown


def test_migration_actions_pin_order_consent_operations_and_no_execution_authority() -> None:
    migration = _topic("concept-migration")
    action_block = next(block for block in migration.blocks if isinstance(block, ActionList))

    assert [(str(action.id), action.consent) for action in action_block.actions] == [
        ("preserve-migration-inputs", ConsentBoundary.MUTATE_AGENTWORKS),
        ("edit-one-manifest", ConsentBoundary.MUTATE_AGENTWORKS),
        ("validate-manifest-set", ConsentBoundary.EXAMINE_WORKSTATION),
        ("review-null-secret-fields", ConsentBoundary.READ_CONFIGURED_STATE),
        ("remove-retired-sections", ConsentBoundary.MUTATE_AGENTWORKS),
        ("compare-operator-inventory", ConsentBoundary.READ_CONFIGURED_STATE),
        ("finish-doctor", ConsentBoundary.EXAMINE_WORKSTATION),
    ]
    assert action_block.actions[0].command is None
    assert action_block.actions[0].manual_steps is not None
    assert action_block.actions[2].command == ("agw", "doctor")
    assert action_block.actions[5].command == (
        "agw",
        "resource",
        "list",
        "--origin",
        "operator",
        "--names-only",
    )


def test_migration_teaching_covers_cutover_validation_backends_and_null_secret_choices() -> None:
    migration = _topic("concept-migration")
    markdown = "\n".join(block.markdown for block in migration.blocks if hasattr(block, "markdown"))
    flowed = " ".join(markdown.split())

    for required in (
        "untouched copies",
        "expected resource names",
        "one manifest at a time",
        "after each edit",
        "`[secret_backends.*]`",
        "`[secret_config].backends`",
        "omission and explicit null both select the default secret name",
        "Azure and AWS also support ambient authentication",
        "Proxmox has no no-secret mode",
        "closed-world fields",
        "strict types",
        "`spec.platform.name: azure-vm`",
        "`spec.provider`",
    ):
        assert required in flowed
    assert "migration command" not in flowed


def test_migration_action_rendering_is_markdown_safe_and_mode_identical() -> None:
    topic = _topic("concept-migration")
    human = render_topic(topic, None, GuideMode.HUMAN)
    agent = render_topic(topic, None, GuideMode.AGENT)

    human_actions = next(block for block in human.blocks if block.key.block_id == "actions")
    agent_actions = next(block for block in agent.blocks if block.key.block_id == "actions")
    assert human_actions.source_payload == agent_actions.source_payload
    assert "Consent boundary: `mutate-agentworks`" in human_actions.markdown
    assert "[secret_config].backends" not in human_actions.markdown
    assert r"\[secret\_config\].backends" in human_actions.markdown
