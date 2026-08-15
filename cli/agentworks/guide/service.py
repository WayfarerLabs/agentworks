"""Request-scoped guide orchestration below the CLI presentation layer."""

from __future__ import annotations

import difflib
import html
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never, cast

from agentworks.errors import AgentworksError, ConfigError, StateError, ValidationError
from agentworks.guide.assessment import OnboardingSnapshot, _validate_verification_evidence
from agentworks.guide.catalog import GuideCatalog, GuideCatalogIssue, _build_guide_catalog
from agentworks.guide.contract import (
    ActionList,
    AgentContract,
    BlockId,
    ConceptAnchor,
    FieldReference,
    GuideBlock,
    GuideContributionError,
    GuideTraversalError,
    ImplementationAnchor,
    InstanceList,
    KindAnchor,
    Overview,
    Relationships,
    ReleaseNotes,
    ResourceAnchor,
    Sample,
    State,
    Teaching,
    TopicContribution,
    TopicLinks,
    TopicSlug,
    UnknownGuideTopicError,
    is_valid_topic_slug,
    parse_topic_contribution,
)
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import framework_heading, render_topic, render_trail_sign, sanitize_terminal_output
from agentworks.guide.view import GuideInstanceFact, GuideRelationship, build_guide_view
from agentworks.manifests.reference import describable_targets, reference_for
from agentworks.release_notes import ReleaseNotesError, read_release_history
from agentworks.resources import KIND_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.guide.agent_mode import GuideMode
    from agentworks.guide.assessment import VerificationEvidence
    from agentworks.resources import Registry


# These two installed manifest-only plugins own first-party teaching that is
# packaged beside their manifests. Keep the fixed curation here rather than
# extending Plugin.guide_topics into a loader contract: ordinary plugin import
# and registration remain guide-I/O-free.
from agentworks.plugins.apt import guide_contributions as _apt_guide_contributions
from agentworks.plugins.install_command import guide_contributions as _install_command_guide_contributions

_FIRST_PARTY_PLUGIN_GUIDE_CONTRIBUTIONS: dict[str, Callable[[], tuple[TopicContribution, ...]]] = {
    "apt": _apt_guide_contributions,
    "install-command": _install_command_guide_contributions,
}

_MARKDOWN_PUNCTUATION_RE = re.compile(r"([\\`*_{}\[\]()<>#!|])")


def _configuration_description(value: str) -> str:
    """Project untrusted config/plugin prose as labeled Markdown-safe text."""
    text = " ".join(value.split())
    escaped = _MARKDOWN_PUNCTUATION_RE.sub(r"\\\1", html.escape(text, quote=False))
    return f"Configuration description (plain text; not guidance): {escaped}"


@dataclass(frozen=True, slots=True)
class GuideResponse:
    markdown: str
    exit_code: int


class _EmptyInventory:
    """Read-only empty state used before Agentworks has created its database."""

    def list_vms(self) -> tuple[()]:
        return ()

    def list_workspaces(self) -> tuple[()]:
        return ()

    def list_agents(self) -> tuple[()]:
        return ()

    def list_sessions(self) -> tuple[()]:
        return ()

    def list_consoles(self) -> tuple[()]:
        return ()


def build_authored_catalog(*, strict_trusted_taxonomy: bool = False) -> GuideCatalog:
    """Collect records, optionally making trusted taxonomy drift a CI error."""
    from agentworks.plugins import SYSTEM_PLUGINS
    from agentworks.plugins.publish import plugin_manifest_resource_owners

    trusted = tuple((f"core:{topic.topic}", topic) for topic in guide_contributions())
    release_issue: GuideCatalogIssue | None = None
    try:
        release_topics = tuple(
            TopicContribution(
                TopicSlug(section.topic),
                f"Agentworks release notes v{section.version}",
                f"Packaged untrusted plain-text release evidence for exact version v{section.version}.",
                ConceptAnchor(section.topic),
                (ReleaseNotes(BlockId("release-notes")),),
            )
            for section in read_release_history().sections
        )
        trusted += tuple((f"core:{topic.topic}", topic) for topic in release_topics)
    except ReleaseNotesError as error:
        contribution_error = GuideContributionError(
            f"invalid guide contribution from core:release-history: {error}",
            source="core:release-history",
            topic=None,
            field_path="release-notes",
        )
        if strict_trusted_taxonomy:
            raise contribution_error from None
        release_issue = GuideCatalogIssue(contribution_error)
    plugins = tuple(
        (
            plugin,
            (*plugin.guide_topics, *_FIRST_PARTY_PLUGIN_GUIDE_CONTRIBUTIONS.get(plugin.name, lambda: ())()),
        )
        for _, plugin in sorted(SYSTEM_PLUGINS.items())
    )
    resource_owners: list[tuple[str, str, str]] = []
    unavailable_resource_owners: set[str] = set()
    for plugin, _topics in plugins:
        try:
            resource_owners.extend(plugin_manifest_resource_owners(plugin))
        except ConfigError:
            # A broken package cannot establish manifest-backed ownership. The
            # catalog rejects only that plugin's resource topics with an
            # accurate scoped issue while retaining unrelated contributions.
            unavailable_resource_owners.add(plugin.name)
    catalog = _build_guide_catalog(
        trusted,
        plugins,
        tuple(resource_owners),
        strict_trusted_taxonomy=strict_trusted_taxonomy,
        unavailable_plugin_resource_owners=frozenset(unavailable_resource_owners),
    )
    if release_issue is None:
        return catalog
    return GuideCatalog(catalog.topics, (*catalog.issues, release_issue))


def _schema_topic(slug: str) -> TopicContribution:
    """Map one authoritative schema reference through contribution validation."""
    reference = reference_for(slug)
    schema_anchor: dict[str, str]
    blocks: list[dict[str, object]] = []
    if reference.overview is not None:
        blocks.append({"type": "overview", "id": "overview", "markdown": reference.overview})
    if reference.category == "declarable":
        schema_anchor = {"type": "kind", "kind": reference.kind}
        blocks.extend(
            (
                {"type": "instance-list", "id": "inventory"},
                {"type": "field-reference", "id": "fields"},
                {"type": "sample", "id": "sample"},
            )
        )
    elif reference.implementation is None:
        schema_anchor = {"type": "kind", "kind": reference.kind}
        blocks.extend(
            (
                {"type": "field-reference", "id": "fields"},
                {"type": "instance-list", "id": "inventory"},
            )
        )
    else:
        schema_anchor = {"type": "implementation", "kind": reference.kind, "name": reference.implementation}
        blocks.extend(
            (
                {"type": "state", "id": "state"},
                {"type": "relationships", "id": "relationships"},
                {"type": "instance-list", "id": "instances"},
                {"type": "field-reference", "id": "fields"},
            )
        )
    return parse_topic_contribution(
        {
            "topic": slug,
            "title": reference.title or reference.target,
            "summary": reference.summary or f"Schema reference for {reference.target}.",
            "anchor": schema_anchor,
            "blocks": blocks,
            "related_topics": (),
        },
        f"schema:{slug}",
    )


def _dynamic_topic(registry: Registry | None, slug: str) -> TopicContribution:
    if slug in describable_targets():
        return _schema_topic(slug)
    if "/" not in slug:
        handler = KIND_REGISTRY[slug]
        return TopicContribution(
            TopicSlug(slug),
            f"{slug} resources",
            _configuration_description(handler.description) if handler.description else f"Current {slug} resources.",
            KindAnchor(slug),
            (InstanceList(BlockId("inventory")),),
        )
    kind, name = slug.split("/", 1)
    handler = KIND_REGISTRY[kind]
    title = slug if len(slug.encode("utf-8")) <= 256 else name
    summary = f"Current facts for {kind}/{name}."
    if registry is not None:
        resource = registry.lookup(kind, name)
        description = getattr(resource, "description", None)
        if isinstance(description, str) and description.strip():
            summary = _configuration_description(description)
    anchor = ImplementationAnchor(kind, name) if handler.category == "capability" else ResourceAnchor(kind, name)
    return TopicContribution(
        TopicSlug(slug),
        title,
        summary,
        anchor,
        (
            State(BlockId("state")),
            Relationships(BlockId("relationships")),
            InstanceList(BlockId("instances")),
            TopicLinks(BlockId("related")),
        ),
        (TopicSlug(kind),),
    )


def _build_schema_catalog(*, strict: bool = False) -> GuideCatalog:
    """Build config-free schema topics, isolating one invalid target at a time."""
    topics: list[TopicContribution] = []
    issues: list[GuideCatalogIssue] = []
    for target in describable_targets():
        try:
            topics.append(_schema_topic(target))
        except GuideContributionError as error:
            if strict:
                raise
            issues.append(GuideCatalogIssue(error))
        except AgentworksError:
            if strict:
                raise
            source = f"schema:{target}"
            issues.append(
                GuideCatalogIssue(
                    GuideContributionError(
                        f"invalid guide contribution from {source}: reference is unavailable",
                        source=source,
                        topic=target,
                        field_path="reference",
                    )
                )
            )
    return GuideCatalog(tuple(topics), tuple(issues))


def _dynamic_names(registry: Registry | None, schema: GuideCatalog | None = None) -> tuple[str, ...]:
    schema = schema or _build_schema_catalog()
    schema_targets = frozenset(describable_targets())
    candidates = set(schema.names())
    if registry is not None:
        candidates.update(
            f"{kind}/{name}"
            for kind in sorted(KIND_REGISTRY)
            for name, _resource in sorted(registry.iter_kind_items(kind))
            if f"{kind}/{name}" not in schema_targets
        )
    return tuple(sorted(name for name in candidates if is_valid_topic_slug(name)))


def build_onboarding_snapshot(registry: Registry, db: Database) -> OnboardingSnapshot:
    """Compose bounded onboarding facts through real anchor-scoped guide views."""
    resources = []
    instances: dict[GuideInstanceFact, None] = {}
    relationships: dict[GuideRelationship, None] = {}
    for kind in sorted(KIND_REGISTRY):
        for name, _resource in sorted(registry.iter_kind_items(kind)):
            view = build_guide_view(_dynamic_topic(registry, f"{kind}/{name}"), registry, db)
            resources.append(view.me())
            for instance in view.instances():
                instances.setdefault(instance, None)
            for relationship in (*view.outbound(), *view.inbound()):
                relationships.setdefault(relationship, None)
    return OnboardingSnapshot(tuple(resources), tuple(instances), tuple(relationships))


def _normalize_requested(requested: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for slug in requested:
        if not is_valid_topic_slug(slug):
            raise ValidationError(f"invalid guide topic {slug!r}")
        if slug not in normalized:
            normalized.append(slug)
    return tuple(normalized)


def _framed_error(error: AgentworksError) -> str:
    message = f"Configuration error: {error}" if isinstance(error, ConfigError) else f"Error: {error}"
    if error.hint:
        message += f"\nHint: {error.hint}"
    return message


def _unknown(slug: str, names: tuple[str, ...]) -> UnknownGuideTopicError:
    return UnknownGuideTopicError(slug, tuple(difflib.get_close_matches(slug, names, n=3)))


def _requires_live_context(block: GuideBlock) -> bool:
    """Classify the closed guide block vocabulary by its data source."""
    if isinstance(block, (InstanceList, State, Relationships)):
        return True
    if isinstance(
        block,
        (Overview, Teaching, AgentContract, ActionList, TopicLinks, ReleaseNotes, FieldReference, Sample),
    ):
        return False
    assert_never(block)


def _topic_requires_live_context(topic: TopicContribution) -> bool:
    return topic.topic == "concept-onboarding" or any(_requires_live_context(block) for block in topic.blocks)


def _live_block_identities(topic: TopicContribution) -> tuple[str, ...]:
    identities = tuple(f"{topic.topic}/{block.id}" for block in topic.blocks if _requires_live_context(block))
    if topic.topic == "concept-onboarding":
        identities += ("concept-onboarding/derived-plan",)
    return identities


def _context_warning(problems: dict[str, dict[str, None]]) -> str:
    if not problems:
        return ""
    rows = []
    for problem, identities in problems.items():
        omitted = ", ".join(f"`{identity}`" for identity in identities)
        rows.append(f"- {problem} Omitted blocks: {omitted}.")
    return f"{framework_heading('Guide context is incomplete')}\n\n" + "\n".join(rows)


def render_guide(
    requested: tuple[str, ...],
    mode: GuideMode,
    *,
    names_only: bool = False,
    load_config_fn: Callable[[], Config] | None = None,
    load_registry_fn: Callable[[Config], Registry] | None = None,
    db: Database | None = None,
    verification_evidence: tuple[VerificationEvidence, ...] = (),
) -> GuideResponse:
    """Build and render one atomic guide request with fail-soft live facts."""
    requested = _normalize_requested(requested)
    if verification_evidence != ():
        if names_only:
            raise ValidationError("verification evidence cannot be used with --names-only")
        if "concept-onboarding" not in requested:
            raise ValidationError("verification evidence requires the concept-onboarding topic")
        _validate_verification_evidence(verification_evidence)
    if not requested and not names_only:
        return GuideResponse(render_trail_sign(mode), 0)

    authored = build_authored_catalog()
    schema = _build_schema_catalog()
    if load_config_fn is None:
        from agentworks.config import load_config

        def load_config_for_guide() -> Config:
            return load_config(raise_errors=True)

        load_config_fn = load_config_for_guide
    if load_registry_fn is None:
        from agentworks.bootstrap import load_guide_registry

        load_registry_fn = load_guide_registry

    authored_names = frozenset(authored.names())
    schema_names = frozenset(schema.names())
    static_names = tuple(sorted(authored_names | schema_names))
    registry: Registry | None = None
    system_error: AgentworksError | None = None
    if names_only:
        try:
            config = load_config_fn()
            loaded_registry = load_registry_fn(config)
            if not loaded_registry.is_finalized:
                raise StateError("resource registry is unavailable")
            registry = loaded_registry
        except AgentworksError:
            pass
        dynamic_names = _dynamic_names(registry, schema)
        all_names = tuple(sorted(authored_names | frozenset(dynamic_names)))
        return GuideResponse("".join(f"{name}\n" for name in all_names), 0)

    validated_slots: list[tuple[str, TopicContribution | str | None]] = []
    rejected_topics = frozenset(
        issue.error.topic for issue in (*authored.issues, *schema.issues) if issue.error.topic is not None
    )
    for slug in requested:
        if slug in authored_names:
            validated_slots.append((slug, authored.lookup(slug)))
        elif slug in schema_names:
            validated_slots.append((slug, schema.lookup(slug)))
        elif slug in rejected_topics:
            validated_slots.append((slug, None))
        elif slug.split("/", 1)[0] in KIND_REGISTRY and slug.count("/") <= 1:
            validated_slots.append((slug, slug))
        else:
            raise _unknown(slug, static_names)

    needs_live_context = any(
        isinstance(value, str) or value is not None and _topic_requires_live_context(value)
        for _slug, value in validated_slots
    )
    if needs_live_context:
        try:
            config = load_config_fn()
            registry = load_registry_fn(config)
        except AgentworksError as error:
            system_error = error
    if registry is not None and not registry.is_finalized:
        raise GuideTraversalError("guide facts require an already-finalized registry")

    all_names = static_names
    if registry is not None:
        dynamic_names = _dynamic_names(registry, schema)
        all_names = tuple(sorted(authored_names | frozenset(dynamic_names)))
        for slug, topic in validated_slots:
            if isinstance(topic, str) and slug not in dynamic_names:
                raise _unknown(slug, all_names)

    requested_slots: list[tuple[str, TopicContribution | None]] = []
    unavailable_topics: set[str] = set()
    # Lookup-time and projection-time absence intentionally report the same
    # operator fact: the requested live resource could not be established.
    for slug, value in validated_slots:
        if not isinstance(value, str):
            requested_slots.append((slug, value))
            continue
        try:
            requested_slots.append((slug, _dynamic_topic(registry, value)))
        except KeyError:
            requested_slots.append((slug, _dynamic_topic(None, value)))
            unavailable_topics.add(slug)

    selected_topics = tuple(topic for _slug, topic in requested_slots if topic is not None)

    visible_issues = tuple(issue for issue in authored.issues if issue.error.topic in requested)
    visible_schema_issues = tuple(issue for issue in schema.issues if issue.error.topic in requested)
    content_issues = [str(issue.error) for issue in visible_schema_issues]
    context_problems: dict[str, dict[str, None]] = {}

    def record_context_problem(error: AgentworksError, identities: tuple[str, ...]) -> None:
        problem = _framed_error(error).replace("\n", " ")
        omitted = context_problems.setdefault(problem, {})
        for identity in identities:
            omitted.setdefault(identity, None)

    for topic in selected_topics:
        if topic.topic in unavailable_topics:
            record_context_problem(
                StateError("requested guide resource is unavailable"),
                _live_block_identities(topic),
            )

    owned_db = False
    if needs_live_context and registry is not None and system_error is None and db is None:
        from agentworks.db import DB_PATH, Database

        if DB_PATH.exists():
            try:
                db = Database(read_only=True)
                owned_db = True
            except AgentworksError as error:
                system_error = error
        else:
            db = cast("Database", _EmptyInventory())

    all_live_identities = tuple(identity for topic in selected_topics for identity in _live_block_identities(topic))
    if system_error is not None:
        record_context_problem(system_error, all_live_identities)

    from agentworks.db import DatabaseDriverError
    from agentworks.db.database import _is_read_only_error

    views = {}
    onboarding_snapshot = None
    onboarding_unavailable = any(topic.topic == "concept-onboarding" for topic in selected_topics)
    try:
        if needs_live_context and registry is not None and system_error is None:
            if db is None:
                raise RuntimeError("guide database was not constructed")
            onboarding_topic = next(
                (topic for topic in selected_topics if topic.topic == "concept-onboarding"),
                None,
            )
            if onboarding_topic is not None:
                try:
                    onboarding_snapshot = build_onboarding_snapshot(registry, db)
                    onboarding_unavailable = False
                except (GuideContributionError, GuideTraversalError):
                    raise
                except AgentworksError as error:
                    record_context_problem(error, ("concept-onboarding/derived-plan",))
            for topic in selected_topics:
                if not any(_requires_live_context(block) for block in topic.blocks):
                    continue
                if topic.topic in unavailable_topics:
                    continue
                try:
                    views[str(topic.topic)] = build_guide_view(topic, registry, db)
                except GuideContributionError:
                    raise
                except GuideTraversalError:
                    anchor = topic.anchor
                    if not isinstance(anchor, (ResourceAnchor, ImplementationAnchor)):
                        raise
                    try:
                        registry.lookup(anchor.kind, anchor.name)
                    except KeyError:
                        unavailable_topics.add(str(topic.topic))
                        record_context_problem(
                            StateError("requested guide resource is unavailable"),
                            _live_block_identities(topic),
                        )
                    else:
                        raise
                except AgentworksError as error:
                    unavailable_topics.add(str(topic.topic))
                    identities = tuple(
                        f"{topic.topic}/{block.id}" for block in topic.blocks if _requires_live_context(block)
                    )
                    record_context_problem(error, identities)
    except DatabaseDriverError as error:
        if _is_read_only_error(error):
            raise GuideTraversalError("guide projection attempted to mutate read-only state") from None
        system_error = StateError(
            "state database rejected a guide projection",
            hint="Run a normal Agentworks command to inspect or repair the state database.",
        )
        views.clear()
        unavailable_topics = {str(topic.topic) for topic in selected_topics if _topic_requires_live_context(topic)}
        onboarding_snapshot = None
        onboarding_unavailable = any(topic.topic == "concept-onboarding" for topic in selected_topics)
        context_problems.clear()
        record_context_problem(system_error, all_live_identities)
    finally:
        if owned_db and db is not None:
            db.close()

    if onboarding_snapshot is not None and verification_evidence:
        from agentworks.guide.assessment import assess_onboarding

        assess_onboarding(onboarding_snapshot, verification_evidence=verification_evidence)

    documents = []
    for slug, topic in requested_slots:
        if topic is None:
            documents.append(f"# {slug}\n\nThis guide topic is unavailable.")
            continue
        topic_name = str(topic.topic)
        live_unavailable = system_error is not None or topic_name in unavailable_topics
        rendered_topic = render_topic(
            topic,
            views.get(topic_name),
            mode,
            live_facts_unavailable=live_unavailable,
            onboarding_snapshot=onboarding_snapshot if topic.topic == "concept-onboarding" else None,
            onboarding_unavailable=topic.topic == "concept-onboarding" and onboarding_unavailable,
            verification_evidence=verification_evidence,
        )
        content_issues.extend(rendered_topic.issues)
        documents.append(rendered_topic.markdown.rstrip())

    markdown = "\n\n---\n\n".join(documents)
    warning = _context_warning(context_problems)
    if warning:
        markdown = f"{warning}\n\n{markdown}"
    markdown += "\n"
    issue_details = [f"- {issue.error.source}: {issue.error}" for issue in visible_issues]
    issue_details.extend(f"- {issue}" for issue in dict.fromkeys(content_issues))
    issue_markdown = ""
    if issue_details:
        details = "\n".join(issue_details)
        issue_markdown = f"\n\n{framework_heading('Guide content unavailable')}\n\n{details}"
    exit_code = 1 if visible_issues or content_issues else 0
    output = sanitize_terminal_output(markdown.rstrip() + issue_markdown + "\n")
    return GuideResponse(output, exit_code)
