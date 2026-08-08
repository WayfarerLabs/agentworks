"""Trusted, authored guide contributions shipped with Agentworks."""

from __future__ import annotations

from importlib.resources import files

from agentworks.guide.contract import (
    ActionId,
    ActionInput,
    ActionList,
    AgentContract,
    BlockId,
    ConceptAnchor,
    ConsentBoundary,
    GuideAction,
    GuideBlock,
    InstanceList,
    Overview,
    Teaching,
    TopicContribution,
    TopicLinks,
    TopicSlug,
)


def _markdown(topic: str, block_id: str) -> str:
    resource = files("agentworks.guide").joinpath("guide-content", topic, f"{block_id}.md")
    return resource.read_text(encoding="utf-8").strip()


def _concept(
    slug: str,
    title: str,
    summary: str,
    *,
    inventory: bool = False,
    related_topics: tuple[str, ...] = (),
    actions: tuple[GuideAction, ...] = (),
) -> TopicContribution:
    blocks: tuple[GuideBlock, ...] = (
        Overview(BlockId("overview"), _markdown(slug, "overview")),
        AgentContract(BlockId("agent-contract"), _markdown(slug, "agent-contract")),
        Teaching(BlockId("teaching"), _markdown(slug, "teaching")),
    )
    if inventory:
        blocks += (InstanceList(BlockId("inventory")),)
    if actions:
        blocks += (ActionList(BlockId("actions"), actions),)
    if related_topics:
        blocks += (TopicLinks(BlockId("related")),)
    return TopicContribution(
        TopicSlug(slug),
        title,
        summary,
        ConceptAnchor(slug),
        blocks,
        tuple(TopicSlug(topic) for topic in related_topics),
    )


def _migration_actions() -> tuple[GuideAction, ...]:
    """Return the bounded, inert operations taught by the migration topic."""
    return (
        GuideAction(
            ActionId("inventory-retired-resources"),
            "No migration backup or edit has begun.",
            (ActionInput("CONFIG_PATH", "The config.toml file selected for read-only inventory.", True),),
            ConsentBoundary.READ_CONFIGURED_STATE,
            None,
            "The caller owns the expected identity set initialized with one canonical kind/name for every "
            "manifest-producing "
            "retired section, with nested subtables collapsed into their parent and secret_backends "
            "declarations excluded.",
            None,
            "Stop before backup or editing and leave CONFIG_PATH unread.",
            "Read only CONFIG_PATH. Record one canonical kind/name for each manifest-producing retired "
            "section, collapse nested subtables into the parent resource, and exclude every "
            "[secret_backends.*] declaration.",
        ),
        GuideAction(
            ActionId("backup-config"),
            "The caller-owned expected identities include every retired TOML resource and no migration edit has begun.",
            (
                ActionInput("CONFIG_PATH", "The config.toml file selected for migration.", True),
                ActionInput(
                    "CONFIG_BACKUP_PATH",
                    "A fresh operator-selected destination outside the active config and resources trees.",
                    True,
                ),
            ),
            ConsentBoundary.MUTATE_AGENTWORKS,
            None,
            "CONFIG_BACKUP_PATH is fresh, distinct from CONFIG_PATH, outside the active config and resources "
            "trees, and contains an untouched copy of CONFIG_PATH.",
            None,
            "Leave CONFIG_PATH unchanged and stop migration before any edit.",
            "Confirm CONFIG_BACKUP_PATH does not exist, is distinct from CONFIG_PATH, and is outside the active "
            "config and resources trees. Then copy CONFIG_PATH there with an operator-selected platform-native "
            "tool.",
        ),
        GuideAction(
            ActionId("backup-resources"),
            "The config backup exists and no migration edit has begun.",
            (
                ActionInput(
                    "RESOURCES_PATH",
                    "The active resource manifest directory path, whether it currently exists or is absent.",
                    True,
                ),
                ActionInput(
                    "RESOURCES_BACKUP_PATH",
                    "A fresh operator-selected destination outside the active trees when RESOURCES_PATH exists.",
                    False,
                ),
            ),
            ConsentBoundary.MUTATE_AGENTWORKS,
            None,
            "When RESOURCES_PATH exists, RESOURCES_BACKUP_PATH is fresh, distinct, outside the active config "
            "and resources trees, and contains an untouched copy. Otherwise, the caller owns an explicit absent "
            "resources baseline and no directory is created.",
            None,
            "Leave RESOURCES_PATH unchanged and stop migration before any edit.",
            "If RESOURCES_PATH exists, confirm the selected RESOURCES_BACKUP_PATH is fresh, distinct, and "
            "outside the active config and resources trees, then copy the directory there with an "
            "operator-selected platform-native tool. If RESOURCES_PATH is absent, record an explicit absent "
            "resources baseline without creating either directory.",
        ),
        GuideAction(
            ActionId("verify-migration-inputs"),
            "Both backup actions completed and the caller owns the expected identities initialized from TOML.",
            (
                ActionInput("CONFIG_PATH", "The source config.toml file.", True),
                ActionInput("CONFIG_BACKUP_PATH", "The config backup to verify.", True),
                ActionInput("RESOURCES_PATH", "The source resource manifest directory.", True),
                ActionInput(
                    "RESOURCES_BACKUP_PATH",
                    "The resources backup to verify when the source existed; omit for an absent baseline.",
                    False,
                ),
                ActionInput(
                    "EXPECTED_IDENTITIES",
                    "The caller-owned identity set initialized from retired TOML sections.",
                    True,
                ),
            ),
            ConsentBoundary.READ_CONFIGURED_STATE,
            None,
            "CONFIG_BACKUP_PATH matches CONFIG_PATH byte for byte, RESOURCES_BACKUP_PATH has exactly the "
            "same paths and file bytes as an existing RESOURCES_PATH or both match the explicit absent baseline. "
            "The caller-owned EXPECTED_IDENTITIES set is extended with every pre-existing manifest's "
            "kind/name, operator-declared origin variant, and manifest file path. Source line is "
            "ignored because multi-document edits can shift it.",
            None,
            "Stop before any edit when a backup differs or EXPECTED_IDENTITIES is incomplete.",
            "Read CONFIG_PATH and CONFIG_BACKUP_PATH and compare them byte for byte. For resources, compare "
            "the exact paths and file bytes or confirm the explicit absent baseline. Derive pre-existing manifest "
            "identities from that baseline and union them into EXPECTED_IDENTITIES. Preserve kind/name, the "
            "operator-declared origin variant, and manifest file path while ignoring source line.",
        ),
        GuideAction(
            ActionId("edit-one-manifest"),
            "Both backups and the caller-owned expected identities passed the read-only verification checkpoint.",
            (
                ActionInput("MANIFEST_PATH", "The single manifest file to create or edit.", True),
                ActionInput("MANIFEST_KIND", "The declarable kind for the live sample and field reference.", True),
                ActionInput(
                    "CAPABILITY_TARGET",
                    "The optional kind/name field-reference target when the manifest has tagged capability config.",
                    False,
                ),
                ActionInput(
                    "EXPECTED_IDENTITIES",
                    "The caller-owned expected identity set updated as this manifest receives a path.",
                    True,
                ),
            ),
            ConsentBoundary.MUTATE_AGENTWORKS,
            None,
            "MANIFEST_PATH contains one resource rewritten against the live sample and field reference for "
            "MANIFEST_KIND and, when present, the separate field reference for CAPABILITY_TARGET. Its expected "
            "identity records kind/name, the operator-declared origin variant, and MANIFEST_PATH while ignoring "
            "source line.",
            None,
            "Keep the last validated manifest set and do not remove any retired TOML section.",
            "Edit only MANIFEST_PATH. Use the live sample and field-reference topic for MANIFEST_KIND. When "
            "tagged capability config is present, also use the separate field-reference topic for "
            "CAPABILITY_TARGET. Update the selected EXPECTED_IDENTITIES entry with the operator-declared origin "
            "variant and MANIFEST_PATH, ignoring source line. Do not copy a schema from this migration topic.",
        ),
        GuideAction(
            ActionId("validate-manifest-set"),
            "One manifest has been added or changed while all retired TOML sections remain in place.",
            (),
            ConsentBoundary.EXAMINE_WORKSTATION,
            ("agw", "doctor"),
            "Doctor gives precise feedback for the growing manifest set even while the retired-section "
            "config error remains.",
            None,
            "Leave the edit unverified, keep every retired TOML section, and block cutover.",
        ),
        GuideAction(
            ActionId("review-null-secret-fields"),
            "Site manifests exist and the three changed secret fields must be classified before cutover.",
            (ActionInput("SITE_MANIFESTS", "The exact site manifest files to inspect.", True),),
            ConsentBoundary.READ_CONFIGURED_STATE,
            None,
            "Each token_secret, service_principal.secret, and credentials.access_key_secret occurrence has "
            "a recorded default, custom-name, or supported ambient-auth intent.",
            None,
            "Leave the site manifests unchanged and block cutover until the intent can be established.",
            "Inspect only SITE_MANIFESTS for token_secret, service_principal.secret, and "
            "credentials.access_key_secret, then record the intended choice for each occurrence.",
        ),
        GuideAction(
            ActionId("remove-retired-sections"),
            "Every manifest validates individually and null-secret choices have been reviewed.",
            (ActionInput("CONFIG_PATH", "The config.toml file selected for final cutover.", True),),
            ConsentBoundary.MUTATE_AGENTWORKS,
            None,
            "CONFIG_PATH has no retired resource sections, and desired secret backends are activated "
            "through [secret_config].backends.",
            None,
            "Restore or retain the untouched config and accept its hard retired-section error.",
            "In one edit, remove every retired resource section and every [secret_backends.*] declaration "
            "from CONFIG_PATH, then update [secret_config].backends with the desired backend names.",
        ),
        GuideAction(
            ActionId("compare-operator-inventory"),
            "The final TOML cutover has loaded successfully.",
            (
                ActionInput(
                    "EXPECTED_IDENTITIES",
                    "The caller-owned union of baseline-manifest and retired-TOML identities.",
                    True,
                ),
            ),
            ConsentBoundary.READ_CONFIGURED_STATE,
            ("agw", "resource", "list", "--origin", "operator"),
            "The operator inventory matches EXPECTED_IDENTITIES exactly by kind/name, operator-declared origin "
            "variant, and intended manifest file path, with source line ignored and no missing or extra resource.",
            None,
            "Stop completion and use the backups to investigate any missing or extra resource.",
        ),
        GuideAction(
            ActionId("finish-doctor"),
            "The operator inventory matches the caller-owned expected identities.",
            (),
            ConsentBoundary.EXAMINE_WORKSTATION,
            ("agw", "doctor"),
            "Doctor reports zero failures for the migrated installation.",
            None,
            "Leave host readiness unverified and do not declare the migration complete.",
        ),
    )


def guide_contributions() -> tuple[TopicContribution, ...]:
    """Load core prose only when a guide request builds its catalog."""
    from agentworks.secrets import guide_contributions as secret_guide_contributions

    return (
        _concept(
            "concept-onboarding",
            "Agentworks onboarding",
            "Start safely, inspect the current system, and take one consented step at a time.",
            inventory=True,
            related_topics=("concept-migration",),
        ),
        _concept(
            "concept-management",
            "Resource management",
            "Use declared resources, capability implementations, and live instances deliberately.",
            related_topics=("concept-migration",),
        ),
        _concept(
            "concept-migration",
            "Resource model migration",
            "Migrate retired 0.14 resource sections into live declarative manifests with explicit checkpoints.",
            related_topics=("concept-onboarding", "concept-management"),
            actions=_migration_actions(),
        ),
        _concept(
            "concept-troubleshooting",
            "Troubleshooting",
            "Diagnose from framed errors and explicit checks before attempting repairs.",
        ),
        _concept(
            "concept-reporting-bugs",
            "Reporting bugs",
            "Prepare a redacted reproduction and obtain approval before submitting it externally.",
        ),
    ) + secret_guide_contributions()
