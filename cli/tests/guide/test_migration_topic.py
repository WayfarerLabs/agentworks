from __future__ import annotations

from agentworks.guide import ActionList, ConsentBoundary, GuideMode, Teaching, TopicLinks
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import render_topic


def _topic(slug: str):
    return next(topic for topic in guide_contributions() if topic.topic == slug)


def test_migration_is_a_colocated_exception_topic_linked_without_teaching_duplication() -> None:
    migration = _topic("concept-migration")
    migration_teaching = next(block.markdown for block in migration.blocks if isinstance(block, Teaching))
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
        ("inventory-retired-resources", ConsentBoundary.READ_CONFIGURED_STATE),
        ("backup-config", ConsentBoundary.MUTATE_AGENTWORKS),
        ("backup-resources", ConsentBoundary.MUTATE_AGENTWORKS),
        ("verify-migration-inputs", ConsentBoundary.READ_CONFIGURED_STATE),
        ("edit-one-manifest", ConsentBoundary.MUTATE_AGENTWORKS),
        ("validate-manifest-set", ConsentBoundary.EXAMINE_WORKSTATION),
        ("review-null-secret-fields", ConsentBoundary.READ_CONFIGURED_STATE),
        ("remove-retired-sections", ConsentBoundary.MUTATE_AGENTWORKS),
        ("compare-operator-inventory", ConsentBoundary.READ_CONFIGURED_STATE),
        ("finish-doctor", ConsentBoundary.EXAMINE_WORKSTATION),
    ]
    assert action_block.actions[0].command is None
    assert action_block.actions[0].manual_steps is not None
    assert action_block.actions[5].command == ("agw", "doctor")
    assert action_block.actions[8].command == (
        "agw",
        "resource",
        "list",
        "--origin",
        "operator",
    )


def test_migration_actions_make_inventory_backups_and_verification_distinct() -> None:
    migration = _topic("concept-migration")
    actions = next(block for block in migration.blocks if isinstance(block, ActionList)).actions
    by_id = {str(action.id): action for action in actions}

    inventory = by_id["inventory-retired-resources"]
    assert [item.name for item in inventory.required_inputs] == ["CONFIG_PATH"]
    assert inventory.manual_steps is not None
    assert "canonical kind/name" in inventory.manual_steps
    assert "collapse nested subtables" in inventory.manual_steps
    assert "exclude every [secret_backends.*]" in inventory.manual_steps

    config_backup = by_id["backup-config"]
    assert [item.name for item in config_backup.required_inputs] == ["CONFIG_PATH", "CONFIG_BACKUP_PATH"]
    assert config_backup.manual_steps is not None
    assert "does not exist" in config_backup.manual_steps
    assert "distinct from CONFIG_PATH" in config_backup.manual_steps
    assert "outside the active config and resources trees" in config_backup.manual_steps

    resources_backup = by_id["backup-resources"]
    assert [(item.name, item.required) for item in resources_backup.required_inputs] == [
        ("RESOURCES_PATH", True),
        ("RESOURCES_BACKUP_PATH", False),
    ]
    assert resources_backup.manual_steps is not None
    assert "explicit absent resources baseline" in resources_backup.manual_steps
    assert "without creating either directory" in resources_backup.manual_steps

    verification = by_id["verify-migration-inputs"]
    assert verification.consent is ConsentBoundary.READ_CONFIGURED_STATE
    assert verification.manual_steps is not None
    assert "compare them byte for byte" in verification.manual_steps
    assert "exact paths and file bytes" in verification.manual_steps
    assert "explicit absent baseline" in verification.manual_steps
    assert "union them into EXPECTED_IDENTITIES" in verification.manual_steps
    assert "operator-declared origin variant" in verification.manual_steps
    assert "manifest file path" in verification.manual_steps
    assert "ignoring source line" in verification.manual_steps

    edit = by_id["edit-one-manifest"]
    assert [(item.name, item.required) for item in edit.required_inputs] == [
        ("MANIFEST_PATH", True),
        ("MANIFEST_KIND", True),
        ("CAPABILITY_TARGET", False),
        ("EXPECTED_IDENTITIES", True),
    ]
    assert edit.manual_steps is not None
    assert "separate field-reference topic" in edit.manual_steps
    assert "operator-declared origin variant and MANIFEST_PATH" in edit.manual_steps

    comparison = by_id["compare-operator-inventory"]
    assert [item.name for item in comparison.required_inputs] == ["EXPECTED_IDENTITIES"]
    assert comparison.command == ("agw", "resource", "list", "--origin", "operator")


def test_migration_mutations_name_exact_scope_and_never_claim_implicit_authority() -> None:
    migration = _topic("concept-migration")
    actions = next(block for block in migration.blocks if isinstance(block, ActionList)).actions
    mutate = tuple(action for action in actions if action.consent is ConsentBoundary.MUTATE_AGENTWORKS)

    assert [str(action.id) for action in mutate] == [
        "backup-config",
        "backup-resources",
        "edit-one-manifest",
        "remove-retired-sections",
    ]
    assert all(action.command is None and action.manual_steps for action in mutate)
    assert "CONFIG_PATH" in (mutate[0].manual_steps or "")
    assert "RESOURCES_PATH" in (mutate[1].manual_steps or "")
    assert "MANIFEST_PATH" in (mutate[2].manual_steps or "")
    assert "CONFIG_PATH" in (mutate[3].manual_steps or "")


def test_manifest_origin_identity_ignores_only_a_shifted_source_line() -> None:
    before = ("vm-template/existing", "operator-declared", "resources/templates.yaml", 8)
    after = ("vm-template/existing", "operator-declared", "resources/templates.yaml", 19)
    comparison = next(
        action
        for block in _topic("concept-migration").blocks
        if isinstance(block, ActionList)
        for action in block.actions
        if action.id == "compare-operator-inventory"
    )

    assert before[:3] == after[:3]
    assert before[3] != after[3]
    assert "manifest file path" in comparison.expected_state
    assert "source line ignored" in comparison.expected_state


def test_migration_teaching_covers_cutover_validation_backends_and_null_secret_choices() -> None:
    migration = _topic("concept-migration")
    markdown = "\n".join(block.markdown for block in migration.blocks if hasattr(block, "markdown"))
    flowed = " ".join(markdown.split())

    for required in (
        "one canonical `kind/name`",
        "fresh operator-selected destinations",
        "explicit absent resources baseline",
        "Extend the caller-owned expected identities with every pre-existing baseline manifest",
        "operator-declared origin variant",
        "Ignore the source line",
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
