"""Request-scoped guide orchestration below the CLI presentation layer."""

from __future__ import annotations

import difflib
import html
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agentworks.errors import AgentworksError, ConfigError, StateError, ValidationError
from agentworks.guide.assessment import OnboardingSnapshot
from agentworks.guide.catalog import GuideCatalog, GuideCatalogIssue, _build_guide_catalog
from agentworks.guide.contract import (
    BlockId,
    GuideContributionError,
    ImplementationAnchor,
    InstanceList,
    KindAnchor,
    Relationships,
    ResourceAnchor,
    State,
    TopicContribution,
    TopicLinks,
    TopicSlug,
    UnknownGuideTopicError,
    is_valid_topic_slug,
    parse_topic_contribution,
)
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import framework_heading, render_index, render_topic, sanitize_terminal_output
from agentworks.guide.view import build_guide_view
from agentworks.manifests.reference import describable_targets, reference_for
from agentworks.resources import KIND_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.guide.agent_mode import GuideMode
    from agentworks.guide.assessment import VerificationEvidence
    from agentworks.resources import Registry

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
    names: tuple[str, ...]


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
    plugins = tuple((plugin, tuple(plugin.guide_topics)) for _, plugin in sorted(SYSTEM_PLUGINS.items()))
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
    return _build_guide_catalog(
        trusted,
        plugins,
        tuple(resource_owners),
        strict_trusted_taxonomy=strict_trusted_taxonomy,
        unavailable_plugin_resource_owners=frozenset(unavailable_resource_owners),
    )


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
    instances = []
    relationships = []
    for kind in sorted(KIND_REGISTRY):
        for name, _resource in sorted(registry.iter_kind_items(kind)):
            view = build_guide_view(_dynamic_topic(registry, f"{kind}/{name}"), registry, db)
            resources.append(view.me())
            for instance in view.instances():
                if instance not in instances:
                    instances.append(instance)
            for relationship in (*view.outbound(), *view.inbound()):
                if relationship not in relationships:
                    relationships.append(relationship)
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


def _view_failure(topic: TopicContribution, error: AgentworksError) -> str:
    """Frame a bounded per-topic projection failure without leaking raw resource objects."""
    return f"live facts for {topic.topic} are unavailable: {_framed_error(error)}"


def _unknown(slug: str, names: tuple[str, ...]) -> UnknownGuideTopicError:
    return UnknownGuideTopicError(slug, tuple(difflib.get_close_matches(slug, names, n=3)))


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
    authored = build_authored_catalog()
    schema = _build_schema_catalog()
    requested = _normalize_requested(requested)
    if verification_evidence != ():
        if names_only:
            raise ValidationError("verification evidence cannot be used with --names-only")
        if not requested:
            raise ValidationError("verification evidence requires the concept-onboarding topic")
        if "concept-onboarding" not in requested:
            raise ValidationError("verification evidence requires the concept-onboarding topic")
    if load_config_fn is None:
        from agentworks.config import load_config

        def load_config_for_guide() -> Config:
            return load_config(raise_errors=True)

        load_config_fn = load_config_for_guide
    if load_registry_fn is None:
        from agentworks.bootstrap import load_guide_registry

        load_registry_fn = load_guide_registry
    registry: Registry | None = None
    system_error: AgentworksError | None = None
    try:
        config = load_config_fn()
        registry = load_registry_fn(config)
    except AgentworksError as error:
        system_error = error

    authored_names = frozenset(authored.names())
    dynamic_names = _dynamic_names(registry, schema)
    dynamic_only_names = tuple(name for name in dynamic_names if name not in authored_names)
    all_names = tuple(sorted(authored_names | frozenset(dynamic_only_names)))
    if names_only:
        return GuideResponse("".join(f"{name}\n" for name in all_names), 0, all_names)

    validated_slots: list[tuple[str, TopicContribution | str | None]] = []
    if requested:
        schema_names = frozenset(schema.names())
        rejected_topics = frozenset(
            issue.error.topic for issue in (*authored.issues, *schema.issues) if issue.error.topic is not None
        )
        for slug in requested:
            if slug in authored_names:
                validated_slots.append((slug, authored.lookup(slug)))
                continue
            if slug in schema_names:
                validated_slots.append((slug, schema.lookup(slug)))
                continue
            if slug in rejected_topics:
                validated_slots.append((slug, None))
                continue
            kind = slug.split("/", 1)[0]
            if registry is not None:
                valid_dynamic = slug in dynamic_names
            else:
                handler = KIND_REGISTRY.get(kind)
                valid_dynamic = slug in dynamic_names or (
                    handler is not None and handler.category == "declarable" and slug.count("/") == 1
                )
            if not valid_dynamic:
                raise _unknown(slug, all_names)
            validated_slots.append((slug, slug))

    requested_slots = tuple(
        (slug, _dynamic_topic(registry, value) if isinstance(value, str) else value) for slug, value in validated_slots
    )

    selected_topics = tuple(topic for _slug, topic in requested_slots if topic is not None)

    visible_issues = authored.issues
    visible_schema_issues = schema.issues
    if requested:
        visible_issues = tuple(issue for issue in visible_issues if issue.error.topic in requested)
        visible_schema_issues = tuple(issue for issue in visible_schema_issues if issue.error.topic in requested)
    runtime_issues = [str(issue.error) for issue in visible_schema_issues]
    if not requested_slots:
        schema_dynamic_topics = tuple(topic for topic in schema.topics if topic.topic not in authored_names)
        schema_dynamic_names = frozenset(str(topic.topic) for topic in schema_dynamic_topics)
        runtime_dynamic_topics = tuple(
            _dynamic_topic(registry, name) for name in dynamic_only_names if name not in schema_dynamic_names
        )
        index_topics = tuple((*authored.topics, *schema_dynamic_topics, *runtime_dynamic_topics))
        markdown = render_index(index_topics, mode)
    else:
        from agentworks.db import DatabaseDriverError

        owned_db = False
        if selected_topics and registry is not None and db is None:
            from agentworks.db import DB_PATH, Database

            if DB_PATH.exists():
                try:
                    db = Database(read_only=True)
                    owned_db = True
                except AgentworksError as error:
                    system_error = error
                    db = cast("Database", _EmptyInventory())
            else:
                db = cast("Database", _EmptyInventory())
        documents = []
        try:
            onboarding_snapshot = None
            if (
                registry is not None
                and system_error is None
                and any(topic.topic == "concept-onboarding" for topic in selected_topics)
            ):
                if db is None:
                    raise RuntimeError("guide database was not constructed")
                onboarding_topic = next(topic for topic in selected_topics if topic.topic == "concept-onboarding")
                try:
                    onboarding_snapshot = build_onboarding_snapshot(registry, db)
                except AgentworksError as error:
                    runtime_issues.append(_view_failure(onboarding_topic, error))
                    onboarding_snapshot = None
                # Validate the complete replay log against current projected
                # targets before rendering any selected document. This keeps
                # multi-topic output atomic even when onboarding is not first.
                if onboarding_snapshot is not None and verification_evidence != ():
                    from agentworks.guide.assessment import assess_onboarding

                    assess_onboarding(onboarding_snapshot, verification_evidence=verification_evidence)
                elif verification_evidence != ():
                    raise ValidationError("verification evidence requires available onboarding facts")
            elif verification_evidence != ():
                raise ValidationError("verification evidence requires available onboarding facts")
            for slug, topic in requested_slots:
                if topic is None:
                    documents.append(f"# {slug}\n\nThis guide topic is unavailable.")
                    continue
                view = None
                unavailable = None
                if registry is not None and system_error is None:
                    if db is None:
                        raise RuntimeError("guide database was not constructed")
                    if topic.topic == "concept-onboarding" and onboarding_snapshot is None:
                        unavailable = "this topic's live projection is unavailable"
                    else:
                        try:
                            view = build_guide_view(topic, registry, db)
                        except AgentworksError as error:
                            runtime_issues.append(_view_failure(topic, error))
                            unavailable = "this topic's live projection is unavailable"
                else:
                    unavailable = "see the system failure below"
                rendered_topic = render_topic(
                    topic,
                    view,
                    mode,
                    unavailable=unavailable,
                    onboarding_snapshot=(onboarding_snapshot if topic.topic == "concept-onboarding" else None),
                    verification_evidence=verification_evidence,
                )
                runtime_issues.extend(rendered_topic.issues)
                documents.append(rendered_topic.markdown.rstrip())
        except DatabaseDriverError:
            # The guide owns a read-only connection. A future projection that
            # accidentally attempts a write must degrade like any other live
            # fact failure, without exposing SQLite internals or a database
            # path. Render the whole selected set without live views so a
            # multi-topic response stays atomic.
            system_error = StateError(
                "state database rejected a guide projection",
                hint="Run a normal Agentworks command to inspect or repair the state database.",
            )
            documents = [
                (
                    f"# {slug}\n\nThis guide topic is unavailable."
                    if topic is None
                    else render_topic(
                        topic,
                        None,
                        mode,
                        unavailable="see the system failure below",
                        verification_evidence=verification_evidence,
                    ).markdown.rstrip()
                )
                for slug, topic in requested_slots
            ]
        finally:
            if owned_db and db is not None:
                db.close()
        markdown = "\n\n---\n\n".join(documents) + "\n"
    issue_details = [f"- {issue.error.source}: {issue.error}" for issue in visible_issues]
    issue_details.extend(f"- {issue}" for issue in dict.fromkeys(runtime_issues))
    issue_markdown = ""
    if issue_details:
        details = "\n".join(issue_details)
        issue_markdown = f"\n\n{framework_heading('Guide content unavailable')}\n\n{details}"
    error_markdown = (
        f"\n\n{framework_heading('Live facts unavailable')}\n\n{_framed_error(system_error)}" if system_error else ""
    )
    exit_code = 1 if visible_issues or runtime_issues or system_error is not None else 0
    output = sanitize_terminal_output(markdown.rstrip() + issue_markdown + error_markdown + "\n")
    return GuideResponse(output, exit_code, all_names)
