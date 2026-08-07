"""Request-scoped guide orchestration below the CLI presentation layer."""

from __future__ import annotations

import difflib
import html
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agentworks.errors import AgentworksError, ConfigError, StateError, ValidationError
from agentworks.guide.assessment import OnboardingSnapshot
from agentworks.guide.catalog import GuideCatalog, _build_guide_catalog
from agentworks.guide.contract import (
    BlockId,
    ImplementationAnchor,
    InstanceList,
    KindAnchor,
    Relationships,
    ResourceAnchor,
    State,
    TopicContribution,
    TopicSlug,
    UnknownGuideTopicError,
)
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import render_index, render_topic, sanitize_terminal_output
from agentworks.guide.view import build_guide_view
from agentworks.resources import KIND_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.guide.agent_mode import GuideMode
    from agentworks.guide.assessment import VerificationEvidence
    from agentworks.resources import Registry

_TOPIC_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}(?:/[a-z][a-z0-9-]{0,62}){0,2}$")
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

    trusted = tuple((f"core:{topic.topic}", topic) for topic in guide_contributions())
    plugins = tuple((plugin, tuple(plugin.guide_topics)) for _, plugin in sorted(SYSTEM_PLUGINS.items()))
    return _build_guide_catalog(trusted, plugins, strict_trusted_taxonomy=strict_trusted_taxonomy)


def _dynamic_topic(registry: Registry | None, slug: str) -> TopicContribution:
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
    title = f"{kind}/{name}"
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
        (State(BlockId("state")), Relationships(BlockId("relationships")), InstanceList(BlockId("instances"))),
    )


def _dynamic_names(registry: Registry) -> tuple[str, ...]:
    return tuple(sorted(KIND_REGISTRY)) + tuple(
        f"{kind}/{name}" for kind in sorted(KIND_REGISTRY) for name, _resource in sorted(registry.iter_kind_items(kind))
    )


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
        if not _TOPIC_RE.fullmatch(slug):
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

    dynamic_names = _dynamic_names(registry) if registry is not None else tuple(sorted(KIND_REGISTRY))
    all_names = tuple(sorted((*authored.names(), *dynamic_names)))
    if names_only:
        return GuideResponse("".join(f"{name}\n" for name in all_names), 0, all_names)

    selected: list[TopicContribution] = []
    if requested:
        for slug in requested:
            if slug in authored.names():
                selected.append(authored.lookup(slug))
                continue
            kind = slug.split("/", 1)[0]
            valid_dynamic = (
                slug in dynamic_names if registry is not None else kind in KIND_REGISTRY and slug.count("/") <= 1
            )
            if not valid_dynamic:
                raise _unknown(slug, all_names)
            selected.append(_dynamic_topic(registry, slug))

    visible_issues = authored.issues
    if requested:
        visible_issues = tuple(issue for issue in authored.issues if issue.error.topic in requested)
    runtime_issues: list[str] = []
    if not selected:
        index_topics = tuple((*authored.topics, *(_dynamic_topic(registry, name) for name in dynamic_names)))
        markdown = render_index(index_topics, mode)
    else:
        owned_db = False
        if registry is not None and db is None:
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
                and any(topic.topic == "concept-onboarding" for topic in selected)
            ):
                if db is None:
                    raise RuntimeError("guide database was not constructed")
                onboarding_topic = next(topic for topic in selected if topic.topic == "concept-onboarding")
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
            for topic in selected:
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
                documents.append(
                    render_topic(
                        topic,
                        view,
                        mode,
                        unavailable=unavailable,
                        onboarding_snapshot=(onboarding_snapshot if topic.topic == "concept-onboarding" else None),
                        verification_evidence=verification_evidence,
                    ).markdown.rstrip()
                )
        except sqlite3.DatabaseError:
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
                render_topic(
                    topic,
                    None,
                    mode,
                    unavailable="see the system failure below",
                    verification_evidence=verification_evidence,
                ).markdown.rstrip()
                for topic in selected
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
        issue_markdown = f"\n\n## Guide content unavailable\n\n{details}"
    error_markdown = f"\n\n## Live facts unavailable\n\n{_framed_error(system_error)}" if system_error else ""
    exit_code = 1 if visible_issues or runtime_issues or system_error is not None else 0
    output = sanitize_terminal_output(markdown.rstrip() + issue_markdown + error_markdown + "\n")
    return GuideResponse(output, exit_code, all_names)
