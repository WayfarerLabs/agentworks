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
            (
                ActionInput("CONFIG_PATH", "The config.toml file selected for read-only inventory.", True),
                ActionInput(
                    "RESOURCES_PATH",
                    "The active resource manifest directory to inventory when it exists; omit when absent.",
                    False,
                ),
                ActionInput(
                    "INTENDED_MANIFEST_PATHS",
                    "The operator-chosen manifest file for every manifest-producing retired TOML identity.",
                    True,
                ),
            ),
            ConsentBoundary.READ_CONFIGURED_STATE,
            None,
            "The caller owns one complete immutable EXPECTED_IDENTITIES union. It includes every pre-existing "
            "manifest's kind/name, operator-declared origin variant, and manifest file path, plus every "
            "manifest-producing retired TOML identity with the same origin variant and its operator-chosen "
            "intended manifest file. Source lines are excluded.",
            None,
            "Stop before backup or editing and leave CONFIG_PATH and RESOURCES_PATH unread.",
            "Read only CONFIG_PATH and, when it exists, RESOURCES_PATH. Record each pre-existing manifest by "
            "kind/name, operator-declared origin variant, and manifest file path. Record each "
            "manifest-producing retired TOML section by canonical kind/name, the operator-declared variant, "
            "and its path from INTENDED_MANIFEST_PATHS. Collapse nested subtables into their parent, exclude "
            "every [secret_backends.*] declaration, omit source lines, and freeze the complete union as "
            "EXPECTED_IDENTITIES before backup or editing.",
        ),
        GuideAction(
            ActionId("backup-config"),
            "The caller-owned EXPECTED_IDENTITIES union is complete and immutable, and no migration edit has begun.",
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
            "EXPECTED_IDENTITIES is immutable, the config backup exists, and no migration edit has begun.",
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
            "Both backup actions completed after EXPECTED_IDENTITIES was frozen.",
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
                    "The complete immutable union established before backup.",
                    True,
                ),
            ),
            ConsentBoundary.READ_CONFIGURED_STATE,
            None,
            "CONFIG_BACKUP_PATH matches CONFIG_PATH byte for byte, RESOURCES_BACKUP_PATH has exactly the "
            "same paths and file bytes as an existing RESOURCES_PATH or both match the explicit absent baseline. "
            "EXPECTED_IDENTITIES still contains exactly the complete pre-backup union, including each "
            "pre-existing manifest and each TOML identity's pre-recorded intended file, without source lines.",
            None,
            "Stop before any edit when a backup differs or EXPECTED_IDENTITIES is incomplete.",
            "Read CONFIG_PATH and CONFIG_BACKUP_PATH and compare them byte for byte. For resources, compare "
            "the exact paths and file bytes or confirm the explicit absent baseline. Validate every entry in "
            "EXPECTED_IDENTITIES against those sources and the pre-recorded intended TOML paths without adding, "
            "removing, or changing any entry.",
        ),
        GuideAction(
            ActionId("edit-one-manifest"),
            "Both backups and the caller-owned expected identities passed the read-only verification checkpoint, "
            "and one pre-existing or TOML-derived manifest is selected for editing.",
            (
                ActionInput(
                    "MANIFEST_PATH",
                    "The pre-recorded file for the selected pre-existing or TOML-derived expected identity.",
                    True,
                ),
                ActionInput(
                    "MANIFEST_KIND",
                    "The pre-recorded declarable kind for the live sample and field reference.",
                    True,
                ),
                ActionInput(
                    "CAPABILITY_TARGET",
                    "The optional kind/name field-reference target when the manifest has tagged capability config.",
                    False,
                ),
                ActionInput(
                    "EXPECTED_IDENTITIES",
                    "The immutable baseline containing this manifest's pre-recorded identity and path.",
                    True,
                ),
            ),
            ConsentBoundary.MUTATE_AGENTWORKS,
            None,
            "MANIFEST_PATH contains one resource rewritten against the live sample and field reference for "
            "MANIFEST_KIND and, when present, the separate field reference for CAPABILITY_TARGET. "
            "Any retired presence shape uses the exact hard-error rewrite, including the outer-null mode mapping. "
            "EXPECTED_IDENTITIES remains byte-for-byte unchanged.",
            None,
            "Keep the last validated manifest set and do not remove any retired TOML section.",
            "Edit only MANIFEST_PATH, whether it is pre-existing or TOML-derived. Use the live sample and "
            "field-reference topic for MANIFEST_KIND. When tagged capability config is present, also use the "
            "separate field-reference topic for CAPABILITY_TARGET. If validation reports a retired "
            "service_principal, credentials, or vm_host shape, apply that hard error's exact rewrite in "
            "MANIFEST_PATH. For an outer explicit null, delete the retired line and write auth ambient, auth "
            "ambient, or placement local, respectively. Require MANIFEST_PATH to equal the selected "
            "EXPECTED_IDENTITIES entry's pre-recorded file. Never add, remove, or change a baseline entry. Do "
            "not copy a schema from this migration topic.",
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
            "Every pre-existing and TOML-derived site manifest exists, and its authentication, placement, "
            "and changed secret-reference intent must be classified before cutover.",
            (
                ActionInput(
                    "SITE_MANIFESTS",
                    "The exact pre-existing and TOML-derived vm-site manifest files to inspect.",
                    True,
                ),
            ),
            ConsentBoundary.READ_CONFIGURED_STATE,
            None,
            "Each current auth and placement mode plus each token_secret, service-principal auth.secret, and "
            "access-key auth.access_key_secret occurrence has a recorded default, custom-name, ambient, or "
            "local intent. Every remaining retired shape has its required exact rewrite recorded for the earlier "
            "edit-one-manifest action, and no file changed during review.",
            None,
            "Leave the site manifests unchanged and block cutover until the intent can be established.",
            "Inspect only SITE_MANIFESTS. Classify Proxmox token_secret, Azure auth.mode and service-principal "
            "auth.secret, AWS auth.mode and access-key auth.access_key_secret, and Lima placement.mode against "
            "the live implementation field references. Omitted auth selects ambient; omitted placement selects "
            "local. An outer explicit null means auth ambient, auth ambient, or placement local, respectively. "
            "Inside a credential arm, an omitted or null secret reference selects its well-known default name. "
            "If a retired service_principal, credentials, or vm_host shape remains, record the hard error's exact "
            "required rewrite and return it to edit-one-manifest. Do not modify a manifest during this review.",
        ),
        GuideAction(
            ActionId("remove-retired-sections"),
            "Every manifest validates individually and all authentication, placement, and secret-reference "
            "choices have been reviewed.",
            (
                ActionInput("CONFIG_PATH", "The config.toml file selected for final cutover.", True),
                ActionInput(
                    "RESOURCES_PATH",
                    "The active resource manifest directory where any required secret-source is declared.",
                    True,
                ),
            ),
            ConsentBoundary.MUTATE_AGENTWORKS,
            None,
            "CONFIG_PATH has no retired resource sections, every desired non-default secret backend has an "
            "operator-declared secret-source, and [secret_config].backends names sources in precedence order.",
            None,
            "Restore or retain the untouched config and accept its hard retired-section error.",
            "In one edit, remove every retired resource section and every [secret_backends.*] declaration "
            "from CONFIG_PATH. Keep implied env-var and prompt names as-is. Declare a secret-source for each "
            "desired non-default backend, move backend config to its tagged spec.backend block, update every "
            "secret mapping key to the source name, and update [secret_config].backends with source names.",
        ),
        GuideAction(
            ActionId("compare-operator-inventory"),
            "The final TOML cutover has loaded successfully.",
            (
                ActionInput(
                    "EXPECTED_IDENTITIES",
                    "The complete immutable union frozen before backup and editing.",
                    True,
                ),
            ),
            ConsentBoundary.EXAMINE_WORKSTATION,
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
