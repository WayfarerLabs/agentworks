"""Request-scoped guide orchestration below the CLI presentation layer."""

from __future__ import annotations

import difflib
import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agentworks.errors import AgentworksError, ConfigError, StateError, ValidationError
from agentworks.guide.assessment import (
    GuideIdentity,
    GuideInstanceFact,
    GuideRelationship,
    GuideResourceFact,
    GuideVerdict,
    OnboardingSnapshot,
    _validate_verification_evidence,
)
from agentworks.guide.catalog import GuideCatalog, GuideCatalogIssue, _build_guide_catalog
from agentworks.guide.contract import (
    BlockId,
    GuideContributionError,
    GuideTraversalError,
    ReleaseNotes,
    TopicContribution,
    TopicSlug,
    UnknownGuideTopicError,
    is_valid_topic_slug,
)
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import framework_heading, render_topic, render_trail_sign, sanitize_terminal_output
from agentworks.release_notes import ReleaseNotesError, read_release_history
from agentworks.resources import KIND_REGISTRY
from agentworks.resources.graph import Enablement

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.guide.agent_mode import GuideMode
    from agentworks.guide.assessment import VerificationEvidence
    from agentworks.plugins.base import Plugin
    from agentworks.resources import Registry


# These two installed manifest-only plugins own first-party teaching packaged
# beside their manifests. Import their private adapters only during a guide
# request: importing a same-named submodule replaces that name on its package,
# while these stable package anchors keep ordinary plugin registration I/O-free.
_FIRST_PARTY_PLUGIN_GUIDE_PACKAGES = {
    "apt": "agentworks.plugins.apt",
    "install-command": "agentworks.plugins.install_command",
}
_ONBOARDING_PLAN_IDENTITY = "concept-onboarding/derived-plan"


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


def _load_first_party_plugin_topics(
    plugin_name: str,
) -> tuple[TopicContribution, ...]:
    """Load one curated package adapter inside guide-scoped construction."""
    package = _FIRST_PARTY_PLUGIN_GUIDE_PACKAGES.get(plugin_name)
    if package is None:
        return ()
    module = importlib.import_module(package)
    loader = cast("Callable[[], tuple[TopicContribution, ...]]", module._load_guide_contributions)
    return loader()


def build_authored_catalog(*, strict: bool = False) -> GuideCatalog:
    """Collect and validate authored core, release, and plugin topics."""
    from agentworks.plugins import SYSTEM_PLUGINS

    trusted = tuple((f"core:{topic.topic}", topic) for topic in guide_contributions())
    release_issue: GuideCatalogIssue | None = None
    try:
        release_topics = tuple(
            TopicContribution(
                TopicSlug(section.topic),
                f"Agentworks release notes v{section.version}",
                f"Packaged untrusted plain-text release evidence for exact version v{section.version}.",
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
        if strict:
            raise contribution_error from None
        release_issue = GuideCatalogIssue(contribution_error)
    plugins: list[tuple[Plugin, tuple[TopicContribution, ...]]] = []
    for _, plugin in sorted(SYSTEM_PLUGINS.items()):
        first_party_topics = _load_first_party_plugin_topics(plugin.name)
        plugins.append((plugin, (*plugin.guide_topics, *first_party_topics)))
    catalog = _build_guide_catalog(trusted, tuple(plugins))
    if release_issue is None:
        return catalog
    return GuideCatalog(catalog.topics, (*catalog.issues, release_issue))


def _resource_fact(registry: Registry, kind: str, name: str) -> GuideResourceFact:
    readiness = registry.graph.readiness_of(kind, name)
    return GuideResourceFact(
        GuideIdentity(kind, name),
        GuideVerdict(
            registry.graph.enablement_of(kind, name) is Enablement.enabled,
            readiness.is_ready,
            readiness.reason,
            is_available=readiness.is_available,
        ),
    )


def build_onboarding_snapshot(registry: Registry, db: Database) -> OnboardingSnapshot:
    """Project the finalized registry and stored instances for onboarding."""
    if not registry.is_finalized:
        raise GuideTraversalError("guide facts require an already-finalized registry")
    resources: list[GuideResourceFact] = []
    instances: dict[GuideInstanceFact, None] = {}
    relationships: dict[GuideRelationship, None] = {}
    for kind, handler in sorted(KIND_REGISTRY.items()):
        for name, _listed_resource in sorted(registry.iter_kind_items(kind)):
            try:
                resource = registry.lookup(kind, name)
            except KeyError:
                raise GuideTraversalError(
                    f"guide resource {kind}/{name} is absent from the finalized registry"
                ) from None
            fact = _resource_fact(registry, kind, name)
            resources.append(fact)
            hook = getattr(handler, "instances", None)
            if hook is not None:
                projected = sorted(
                    (GuideInstanceFact(ref.instance_kind, ref.instance_name) for ref in hook(db, registry, resource)),
                    key=lambda item: (item.kind, item.name),
                )
                for instance in projected:
                    instances.setdefault(instance, None)
            for ref in registry.graph.edges_of(kind, name):
                relationships.setdefault(
                    GuideRelationship(fact.identity, GuideIdentity(ref.kind, ref.name), ref.usage),
                    None,
                )
            for entry in registry.graph.dependents_of(kind, name):
                relationships.setdefault(
                    GuideRelationship(GuideIdentity(*entry.source), fact.identity, entry.usage),
                    None,
                )
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
    authored_names = authored.names()
    if names_only:
        return GuideResponse("".join(f"{name}\n" for name in authored_names), 0)

    rejected_topics = frozenset(issue.error.topic for issue in authored.issues if issue.error.topic is not None)
    requested_slots: list[tuple[str, TopicContribution | None]] = []
    for slug in requested:
        if slug in authored_names:
            requested_slots.append((slug, authored.lookup(slug)))
        elif slug in rejected_topics:
            requested_slots.append((slug, None))
        else:
            raise _unknown(slug, authored_names)

    selected_topics = tuple(topic for _slug, topic in requested_slots if topic is not None)
    onboarding_selected = any(topic.topic == "concept-onboarding" for topic in selected_topics)
    registry: Registry | None = None
    system_error: AgentworksError | None = None
    if onboarding_selected:
        if load_config_fn is None:
            from agentworks.config import load_config

            def load_config_for_guide() -> Config:
                return load_config(raise_errors=True)

            load_config_fn = load_config_for_guide
        if load_registry_fn is None:
            from agentworks.bootstrap import load_guide_registry

            load_registry_fn = load_guide_registry
        try:
            config = load_config_fn()
            registry = load_registry_fn(config)
        except AgentworksError as error:
            system_error = error
    if registry is not None and not registry.is_finalized:
        raise GuideTraversalError("guide facts require an already-finalized registry")

    visible_issues = tuple(issue for issue in authored.issues if issue.error.topic in requested)
    content_issues: list[str] = []
    context_problems: dict[str, dict[str, None]] = {}

    def record_context_problem(error: AgentworksError, identities: tuple[str, ...]) -> None:
        problem = _framed_error(error).replace("\n", " ")
        omitted = context_problems.setdefault(problem, {})
        for identity in identities:
            omitted.setdefault(identity, None)

    owned_db = False
    if onboarding_selected and registry is not None and system_error is None and db is None:
        from agentworks.db import DB_PATH, Database

        if DB_PATH.exists():
            try:
                db = Database(read_only=True)
                owned_db = True
            except AgentworksError as error:
                system_error = error
        else:
            db = cast("Database", _EmptyInventory())

    all_live_identities = (_ONBOARDING_PLAN_IDENTITY,) if onboarding_selected else ()
    if system_error is not None:
        record_context_problem(system_error, all_live_identities)

    from agentworks.db import DatabaseDriverError
    from agentworks.db.database import _is_read_only_error

    onboarding_snapshot = None
    onboarding_unavailable = onboarding_selected
    try:
        if onboarding_selected and registry is not None and system_error is None:
            if db is None:
                raise RuntimeError("guide database was not constructed")
            try:
                onboarding_snapshot = build_onboarding_snapshot(registry, db)
                onboarding_unavailable = False
            except GuideTraversalError:
                raise
            except AgentworksError as error:
                record_context_problem(error, (_ONBOARDING_PLAN_IDENTITY,))
    except DatabaseDriverError as error:
        if _is_read_only_error(error):
            raise GuideTraversalError("guide projection attempted to mutate read-only state") from None
        system_error = StateError(
            "state database rejected a guide projection",
            hint="Run a normal Agentworks command to inspect or repair the state database.",
        )
        onboarding_snapshot = None
        onboarding_unavailable = onboarding_selected
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
        rendered_topic = render_topic(
            topic,
            mode,
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
