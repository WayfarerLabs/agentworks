from __future__ import annotations

import pytest

from agentworks.capabilities.config import capability_config_references, validate_capability_config
from agentworks.errors import ConfigError
from agentworks.guide import ActionList, ConsentBoundary, GuideMode, Teaching, TopicLinks
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import render_topic
from agentworks.manifests.field_tree import FieldEntry
from agentworks.manifests.reference import reference_for
from agentworks.schema import RefOwner


def _topic(slug: str):
    return next(topic for topic in guide_contributions() if topic.topic == slug)


def test_migration_is_a_colocated_exception_topic_linked_without_teaching_duplication() -> None:
    migration = _topic("concept-migration")
    migration_teaching = next(block.markdown for block in migration.blocks if isinstance(block, Teaching))
    assert "`data.counts.unavailable` to equal `0`" in migration_teaching
    assert "An unavailable check leaves host readiness unverified" in migration_teaching
    assert "though doctor itself exits `0`" in migration_teaching
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
        ("compare-operator-inventory", ConsentBoundary.EXAMINE_WORKSTATION),
        ("finish-doctor", ConsentBoundary.EXAMINE_WORKSTATION),
    ]
    assert action_block.actions[0].command is None
    assert action_block.actions[0].manual_steps is not None
    assert action_block.actions[5].command == ("agw", "doctor")
    assert action_block.actions[5].verification == ("agw", "doctor", "--output", "json")
    assert action_block.actions[8].command == (
        "agw",
        "resource",
        "list",
        "--origin",
        "operator",
        "--output",
        "json",
    )
    assert action_block.actions[9].command == ("agw", "doctor", "--output", "json")


def test_migration_actions_make_inventory_backups_and_verification_distinct() -> None:
    migration = _topic("concept-migration")
    actions = next(block for block in migration.blocks if isinstance(block, ActionList)).actions
    by_id = {str(action.id): action for action in actions}

    inventory = by_id["inventory-retired-resources"]
    assert [(item.name, item.required) for item in inventory.required_inputs] == [
        ("CONFIG_PATH", True),
        ("RESOURCES_PATH", False),
        ("INTENDED_MANIFEST_PATHS", True),
    ]
    assert inventory.manual_steps is not None
    assert "pre-existing manifest by kind/name" in inventory.manual_steps
    assert "path from INTENDED_MANIFEST_PATHS" in inventory.manual_steps
    assert "Collapse nested subtables" in inventory.manual_steps
    assert "exclude every [secret_backends.*]" in inventory.manual_steps
    assert "freeze the complete union as EXPECTED_IDENTITIES before backup or editing" in inventory.manual_steps
    assert "complete immutable EXPECTED_IDENTITIES union" in inventory.expected_state
    assert "operator-chosen intended manifest file" in inventory.expected_state

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
    assert "Validate every entry in EXPECTED_IDENTITIES" in verification.manual_steps
    assert "without adding, removing, or changing any entry" in verification.manual_steps

    edit = by_id["edit-one-manifest"]
    assert [(item.name, item.required) for item in edit.required_inputs] == [
        ("MANIFEST_PATH", True),
        ("MANIFEST_KIND", True),
        ("CAPABILITY_TARGET", False),
        ("EXPECTED_IDENTITIES", True),
    ]
    assert edit.manual_steps is not None
    assert "pre-existing or TOML-derived expected identity" in edit.required_inputs[0].description
    assert "separate field-reference topic" in edit.manual_steps
    assert "whether it is pre-existing or TOML-derived" in edit.manual_steps
    assert "apply that hard error's exact rewrite" in edit.manual_steps
    assert "delete the retired line and write auth ambient, auth ambient, or placement local" in edit.manual_steps
    assert "canonical tagged stored arm" in edit.manual_steps
    assert "token: {mode: stored, secret: <existing-name>}" in edit.manual_steps
    assert "Delete an outer token: null line or replace it exactly with token: {mode: stored}" in edit.manual_steps
    assert "an omitted or null inner token.secret selects the default" in edit.manual_steps
    assert "No minted arm exists" in edit.manual_steps
    assert "MANIFEST_PATH to equal" in edit.manual_steps
    assert "pre-recorded file" in edit.manual_steps
    assert "Never add, remove, or change a baseline entry" in edit.manual_steps
    assert "EXPECTED_IDENTITIES remains byte-for-byte unchanged" in edit.expected_state

    validation = by_id["validate-manifest-set"]
    assert validation.command == ("agw", "doctor")
    assert validation.verification == ("agw", "doctor", "--output", "json")
    assert "human report first" in validation.expected_state
    assert "config.toml declares resources" in validation.expected_state
    assert "config.toml is settings only now" in validation.expected_state
    assert "names the retained sections" in validation.expected_state
    assert "Both the human command and JSON verification must exit 1" in validation.expected_state
    assert "parse its one document" in validation.expected_state
    assert "data.groups contains the Configuration group" in validation.expected_state
    assert "Config file with status ok" in validation.expected_state
    assert "Config with status fail" in validation.expected_state
    assert "no check named Manifest or Resource registry" in validation.expected_state
    assert "with status warn or fail" in validation.expected_state
    assert "cannot distinguish the expected Config failure" in validation.expected_state
    assert "only corroborates that stable structural state" in validation.expected_state
    assert "do not prove precise diagnostic detail" in validation.expected_state
    assert "hard error leaves this action unverified" in validation.expected_state
    assert "returns the selected manifest to edit-one-manifest" in validation.expected_state

    comparison = by_id["compare-operator-inventory"]
    assert comparison.consent is ConsentBoundary.EXAMINE_WORKSTATION
    assert [item.name for item in comparison.required_inputs] == ["EXPECTED_IDENTITIES"]
    assert comparison.command == (
        "agw",
        "resource",
        "list",
        "--origin",
        "operator",
        "--output",
        "json",
    )

    finish = by_id["finish-doctor"]
    assert finish.command == ("agw", "doctor", "--output", "json")
    assert "data.counts.fail equals 0" in finish.expected_state
    assert "data.counts.unavailable equals 0" in finish.expected_state
    assert "command exits 0" in finish.expected_state


def test_only_inventory_can_author_an_expected_manifest_path() -> None:
    actions = next(block for block in _topic("concept-migration").blocks if isinstance(block, ActionList)).actions
    inventory, *later = actions

    assert "operator-chosen intended manifest file" in inventory.expected_state
    for action in later:
        authored = " ".join(
            (
                action.precondition,
                action.expected_state,
                action.refusal_alternative,
                action.manual_steps or "",
                *(item.description for item in action.required_inputs),
            )
        )
        assert "operator-chosen intended manifest file" not in authored
        assert "path from INTENDED_MANIFEST_PATHS" not in authored
        assert "update the selected EXPECTED_IDENTITIES" not in authored
        assert "union them into EXPECTED_IDENTITIES" not in authored


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


def _schema_paths(target: str) -> set[tuple[str, ...]]:
    reference = reference_for(target)
    found: set[tuple[str, ...]] = set()

    def visit(entries: tuple[FieldEntry, ...], prefix: tuple[str, ...] = ()) -> None:
        for entry in entries:
            path = (*prefix, entry.name)
            found.add(path)
            visit(entry.children, path)
            for alternative in entry.alternatives:
                visit(alternative.fields, path)

    visit(reference.spec)
    if reference.root_value is not None:
        visit((reference.root_value,))
    return found


def _schema_entries(target: str, wanted: tuple[str, ...]) -> tuple[FieldEntry, ...]:
    reference = reference_for(target)
    found: list[FieldEntry] = []

    def visit(entries: tuple[FieldEntry, ...], prefix: tuple[str, ...] = ()) -> None:
        for entry in entries:
            path = (*prefix, entry.name)
            if path == wanted:
                found.append(entry)
            visit(entry.children, path)
            for alternative in entry.alternatives:
                visit(alternative.fields, path)

    visit(reference.spec)
    return tuple(found)


def test_migration_secret_paths_match_the_live_implementation_references() -> None:
    paths = {
        target: _schema_paths(target)
        for target in (
            "vm-platform/proxmox",
            "vm-platform/azure-vm",
            "vm-platform/aws-ec2",
            "vm-platform/lima",
        )
    }

    assert ("token_secret",) in paths["vm-platform/proxmox"]
    assert ("auth", "secret") in paths["vm-platform/azure-vm"]
    assert ("auth", "access_key_secret") in paths["vm-platform/aws-ec2"]
    assert ("placement", "host") in paths["vm-platform/lima"]
    assert ("service_principal", "secret") not in paths["vm-platform/azure-vm"]
    assert ("credentials", "access_key_secret") not in paths["vm-platform/aws-ec2"]
    assert ("vm_host",) not in paths["vm-platform/lima"]


@pytest.mark.parametrize(
    ("provider", "base"),
    [pytest.param("github", {}, id="github"), pytest.param("azdo", {"org": "acme"}, id="azdo")],
)
def test_migration_git_token_teaching_matches_live_reference_and_services(
    provider: str,
    base: dict[str, object],
) -> None:
    target = f"git-credential-provider/{provider}"
    token_entries = _schema_entries(target, ("token",))
    assert len(token_entries) == 1
    token = token_entries[0]
    assert token.doc.default == {"mode": "stored"}
    assert not token.doc.required
    assert [alternative.name for alternative in token.alternatives] == ["stored"]
    assert _schema_paths(target) >= {("token",), ("token", "mode"), ("token", "secret")}
    assert "minted" not in repr(reference_for(target))

    owner = RefOwner(kind="git-credential", name="dev")

    def refs(token_value: object = ...) -> list[tuple[str, str]]:
        config = {"name": provider, **base}
        if token_value is not ...:
            config["token"] = token_value
        return [
            (ref.kind, ref.name)
            for ref in capability_config_references(
                kind="git-credential-provider",
                config=config,
                owner=owner,
            )
        ]

    assert refs() == [("secret", "git-token-dev")]
    assert refs("gh-pat") == [("secret", "gh-pat")]
    assert refs({"mode": "stored"}) == [("secret", "git-token-dev")]
    assert refs({"mode": "stored", "secret": None}) == [("secret", "git-token-dev")]

    with pytest.raises(ConfigError, match=r"replace the null line with the explicit choice: token: \{mode: stored\}"):
        validate_capability_config(
            kind="git-credential-provider",
            config={"name": provider, **base, "token": None},
            owner=owner,
        )
    with pytest.raises(ConfigError, match="unknown mode 'minted'; registered: 'stored'"):
        validate_capability_config(
            kind="git-credential-provider",
            config={"name": provider, **base, "token": {"mode": "minted"}},
            owner=owner,
        )


def test_declarable_git_credential_reference_keeps_structural_token_union() -> None:
    paths = _schema_paths("git-credential")
    token_entries = _schema_entries("git-credential", ("provider", "token"))

    assert paths >= {
        ("provider",),
        ("provider", "token"),
        ("provider", "token", "mode"),
        ("provider", "token", "secret"),
    }
    assert len(token_entries) == 1
    assert token_entries[0].doc.default == {"mode": "stored"}
    assert [alternative.name for alternative in token_entries[0].alternatives] == ["stored"]


def test_migration_review_action_covers_all_sites_and_distinguishes_outer_from_inner_null() -> None:
    actions = next(block for block in _topic("concept-migration").blocks if isinstance(block, ActionList)).actions
    review = next(action for action in actions if action.id == "review-null-secret-fields")
    manual = review.manual_steps or ""

    assert "pre-existing and TOML-derived vm-site manifest files" in review.required_inputs[0].description
    assert "service-principal auth.secret" in manual
    assert "access-key auth.access_key_secret" in manual
    assert "Omitted auth selects ambient" in manual
    assert "omitted placement selects local" in manual
    assert "outer explicit null means auth ambient, auth ambient, or placement local" in manual
    assert "Inside a credential arm, an omitted or null secret reference" in manual
    assert "record the hard error's exact required rewrite and return it to edit-one-manifest" in manual
    assert "Do not modify a manifest during this review" in manual
    assert "apply that hard error's exact rewrite" not in manual
    assert "delete the retired line" not in manual
    assert "provider.token" not in manual
    assert "token: null" not in manual
    for stale in ("service_principal.secret", "credentials.access_key_secret"):
        assert stale not in manual


def test_migration_teaching_covers_cutover_validation_backends_and_auth_choices() -> None:
    migration = _topic("concept-migration")
    markdown = "\n".join(block.markdown for block in migration.blocks if hasattr(block, "markdown"))
    flowed = " ".join(markdown.split())

    for required in (
        "read only the selected `config.toml` and the resources directory when it exists",
        "operator-chosen intended manifest file",
        "complete union as immutable expected identities",
        "fresh operator-selected destinations",
        "explicit absent resources baseline",
        "operator-declared origin variant",
        "Verification does not add, remove, or change an entry",
        "pre-recorded intended path",
        "without changing the baseline",
        "one manifest at a time",
        "after each edit",
        "must say that `config.toml` declares resources",
        "`config.toml` is settings only now",
        "name the retained sections",
        "`Resource registry` rows for precise diagnostics",
        "Both this human command and the JSON verification",
        "must exit `1` for the retained-section checkpoint",
        "`Config file` with status `ok`",
        "`Config` with status `fail`",
        "no check named `Manifest` or `Resource registry`",
        "cannot distinguish the expected retained-section Config failure",
        "only corroborates that stable structural state",
        "do not prove precise diagnostic detail",
        "`[secret_backends.*]`",
        "`[secret_config].backends`",
        "Inspect every pre-existing and TOML-derived site manifest",
        "Omitted `auth` defaults to ambient authentication",
        "`auth.secret` names the client secret",
        "`auth.access_key_secret` names the secret access key",
        "Omitted `placement` defaults to local placement",
        "`placement.host`",
        "`service_principal: null` means `auth: {mode: ambient}`",
        "`credentials: null` means `auth: {mode: ambient}`",
        "`vm_host: null` means `placement: {mode: local}`",
        "Do not confuse these outer-null mappings with a null inner secret reference",
        "return that manifest to `edit-one-manifest`; do not modify it during review",
        "Proxmox has no no-secret mode",
        "closed-world fields",
        "strict types",
        "`spec.platform.name: azure-vm`",
        "`spec.provider`",
        "An omitted `provider.token` still selects a stored token and the default secret name",
        "A scalar such as `token: gh-pat` is still accepted as shorthand",
        "canonical current spelling is the tagged shape",
        "omitting `token.secret` or writing `secret: null` selects the default secret name",
        "The old outer spelling `token: null` is retired",
        "`token: {mode: stored}`",
        "A retired TOML scalar may still become the accepted scalar shorthand",
        "No `minted` arm exists in the current contract",
        "Any manifest validation hard error returns that manifest to this edit loop",
    ):
        assert required in flowed
    for stale in ("`service_principal.secret`", "`credentials.access_key_secret`"):
        assert stale not in flowed
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
