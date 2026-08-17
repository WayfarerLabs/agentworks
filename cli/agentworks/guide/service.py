"""Request-scoped orchestration for static Markdown guide topics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import ValidationError
from agentworks.guide.catalog import GuideCatalog, discover_concept_shells
from agentworks.guide.contract import ConceptShell, UnknownGuideTopicError, is_concept_topic
from agentworks.guide.render import render_release_topic, render_shell
from agentworks.guide.trail_sign import render_trail_sign
from agentworks.release_notes import read_release_history, topic_version
from agentworks.terminal import sanitize_terminal_output

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

    from agentworks.guide.agent_mode import GuideMode


@dataclass(frozen=True, slots=True)
class GuideResponse:
    """One complete Markdown response."""

    markdown: str


def _normalize_requested(requested: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for slug in requested:
        if not is_concept_topic(slug) and topic_version(slug) is None:
            raise ValidationError(f"invalid guide topic {slug!r}")
        if slug not in normalized:
            normalized.append(slug)
    return tuple(normalized)


def _release_names() -> tuple[str, ...]:
    return tuple(section.topic for section in read_release_history().sections)


def _all_names(catalog: GuideCatalog) -> tuple[str, ...]:
    return tuple(sorted((*catalog.names(), *_release_names())))


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
    requested: tuple[str, ...],
    mode: GuideMode,
    *,
    names_only: bool = False,
    package_root: Traversable | None = None,
) -> GuideResponse:
    """Render a catalog-free trail sign, topic names, or selected static topics."""
    requested = _normalize_requested(requested)
    if not requested and not names_only:
        return GuideResponse(render_trail_sign(mode))

    catalog = discover_concept_shells(package_root)
    if names_only:
        if requested:
            raise ValidationError("topic arguments cannot be used with --names-only")
        return GuideResponse("".join(f"{name}\n" for name in _all_names(catalog)))

    selected = tuple(_resolve(slug, catalog) for slug in requested)
    documents = tuple(
        render_shell(topic, mode, package_root=package_root)
        if isinstance(topic, ConceptShell)
        else render_release_topic(topic)
        for topic in selected
    )
    markdown = "\n\n---\n\n".join(document.rstrip() for document in documents) + "\n"
    return GuideResponse(sanitize_terminal_output(markdown))
