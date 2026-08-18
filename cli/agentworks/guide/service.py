"""Request-scoped orchestration for static Markdown guide topics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.guide.catalog import GuideCatalog, discover_concept_shells
from agentworks.guide.contract import ConceptShell, UnknownGuideTopicError, is_concept_topic
from agentworks.guide.render import render_release_topic, render_shell
from agentworks.release_notes import read_release_history, topic_version
from agentworks.terminal import sanitize_terminal_output

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

    from agentworks.guide.agent_mode import GuideMode


@dataclass(frozen=True, slots=True)
class GuideResponse:
    """One complete Markdown response."""

    markdown: str


def _validate_topic(topic: str) -> str:
    if not is_concept_topic(topic) and topic_version(topic) is None:
        raise ValidationError(f"invalid guide topic {topic!r}")
    return topic


def _release_names() -> tuple[str, ...]:
    return tuple(section.topic for section in read_release_history().sections)


def _all_names(catalog: GuideCatalog) -> tuple[str, ...]:
    return tuple(sorted((*catalog.names(), *_release_names())))


def _render_index(catalog: GuideCatalog, mode: GuideMode, package_root: Traversable | None) -> str:
    indexed = catalog.indexed_topics()
    rows = "\n".join(f"- `{topic.slug}`: {topic.description}" for topic in indexed)
    omitted = len(catalog.topics) - len(indexed)
    sections = [render_shell(catalog.index, mode, package_root=package_root).rstrip()]
    if rows:
        sections.append(rows)
    noun = "concept" if omitted == 1 else "concepts"
    verb = "is" if omitted == 1 else "are"
    sections.append(f"{omitted} other {noun} {verb} available. Run `agw guide list` to see every topic name.")
    return sanitize_terminal_output("\n\n".join(sections) + "\n")


def _resolve(slug: str, catalog: GuideCatalog) -> ConceptShell | str:
    shell = catalog.lookup(slug)
    if shell is not None:
        return shell
    version = topic_version(slug)
    if version is not None:
        history = read_release_history()
        if version in history.versions:
            return version
    raise UnknownGuideTopicError(slug, catalog.names())


def render_guide(
    topic: str | None,
    mode: GuideMode,
    *,
    package_root: Traversable | None = None,
) -> GuideResponse:
    """Render the shell-backed index or one selected static topic."""
    if topic is not None:
        topic = _validate_topic(topic)
    catalog = discover_concept_shells(package_root)
    if topic is None:
        return GuideResponse(_render_index(catalog, mode, package_root))

    selected = _resolve(topic, catalog)
    markdown = (
        render_shell(selected, mode, package_root=package_root)
        if isinstance(selected, ConceptShell)
        else render_release_topic(selected)
    )
    return GuideResponse(sanitize_terminal_output(markdown))


def list_guide_topics(*, package_root: Traversable | None = None) -> GuideResponse:
    """Emit every static concept and packaged exact release topic name."""
    catalog = discover_concept_shells(package_root)
    return GuideResponse("".join(f"{name}\n" for name in _all_names(catalog)))
