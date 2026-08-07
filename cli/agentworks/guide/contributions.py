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
            ActionId("preserve-migration-inputs"),
            "The retired TOML resource sections have been inventoried and no migration edit has begun.",
            (
                ActionInput("CONFIG_PATH", "The config.toml file selected for migration.", True),
                ActionInput("RESOURCES_PATH", "The resource manifest directory selected for migration.", True),
                ActionInput("EXPECTED_NAMES_PATH", "A new file that will record the retired resource names.", True),
            ),
            ConsentBoundary.MUTATE_AGENTWORKS,
            None,
            "Untouched copies of the config and resources exist, and expected resource names are recorded "
            "from the retired sections.",
            None,
            "Stop before any edit and leave the selected config and resources unchanged.",
            "Copy CONFIG_PATH and RESOURCES_PATH to untouched backup locations, then record every resource "
            "name from the retired sections in EXPECTED_NAMES_PATH.",
        ),
        GuideAction(
            ActionId("edit-one-manifest"),
            "Backups and expected resource names exist, and one retired resource is selected.",
            (
                ActionInput("MANIFEST_PATH", "The single manifest file to create or edit.", True),
                ActionInput("TARGET", "The declarable kind or capability implementation being described.", True),
            ),
            ConsentBoundary.MUTATE_AGENTWORKS,
            None,
            "MANIFEST_PATH contains one resource rewritten against the live sample and field reference for TARGET.",
            None,
            "Keep the last validated manifest set and do not remove any retired TOML section.",
            "Edit only MANIFEST_PATH. Use the live sample and field-reference guide topic for TARGET, "
            "without copying a schema from this migration topic.",
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
            (ActionInput("EXPECTED_NAMES_PATH", "The preserved list of expected resource names.", True),),
            ConsentBoundary.READ_CONFIGURED_STATE,
            ("agw", "resource", "list", "--origin", "operator", "--names-only"),
            "The operator-declared inventory matches every name recorded in EXPECTED_NAMES_PATH, with no "
            "missing or extra resources.",
            None,
            "Restore the backup before further work if the inventory cannot be compared or does not match.",
        ),
        GuideAction(
            ActionId("finish-doctor"),
            "The operator inventory matches the preserved expected names.",
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
