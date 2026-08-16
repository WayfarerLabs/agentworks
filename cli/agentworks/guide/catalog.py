"""Request-scoped guide contribution collection and isolation."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.guide.contract import (
    BrokenTopicLinkError,
    DuplicateTopicError,
    GuideContributionError,
    TopicContribution,
    UnknownGuideTopicError,
    _decoded_contribution,
    parse_topic_contribution,
)

if TYPE_CHECKING:
    from agentworks.plugins.base import Plugin


@dataclass(frozen=True, slots=True)
class _GuideContributionCandidate:
    source: str
    value: TopicContribution
    trusted: bool
    plugin: str | None = None


@dataclass(frozen=True, slots=True)
class GuideCatalogIssue:
    error: GuideContributionError


@dataclass(frozen=True, slots=True)
class GuideCatalog:
    topics: tuple[TopicContribution, ...]
    issues: tuple[GuideCatalogIssue, ...] = ()

    def names(self) -> tuple[str, ...]:
        return tuple(str(topic.topic) for topic in self.topics)

    def lookup(self, topic: str) -> TopicContribution:
        for contribution in self.topics:
            if contribution.topic == topic:
                return contribution
        raise UnknownGuideTopicError(topic, tuple(difflib.get_close_matches(topic, self.names(), n=3)))


def _ownership_error(candidate: _GuideContributionCandidate, topic: TopicContribution) -> GuideContributionError | None:
    if candidate.trusted:
        return None
    plugin = candidate.plugin
    valid_prefix = f"plugin/{plugin}/" if plugin else ""
    slug = str(topic.topic)
    if not plugin or not slug.startswith(valid_prefix):
        return GuideContributionError(
            f"invalid guide contribution from {candidate.source}: topic is not owned by plugin {plugin!r}",
            source=candidate.source,
            topic=slug,
            field_path="topic",
        )
    return None


def _issue_key(error: GuideContributionError) -> tuple[str, str, str, str]:
    return (error.source, error.topic or "", error.field_path, str(error))


def _build_guide_catalog(
    trusted: tuple[tuple[str, TopicContribution], ...],
    system_plugins: tuple[tuple[Plugin, tuple[TopicContribution, ...]], ...] = (),
) -> GuideCatalog:
    """Build one authored guide catalog and isolate invalid plugin topics."""
    candidates = tuple(_GuideContributionCandidate(source, value, True) for source, value in trusted) + tuple(
        _GuideContributionCandidate(
            f"system-plugin:{plugin.name}",
            value,
            False,
            plugin.name,
        )
        for plugin, values in system_plugins
        for value in values
    )
    parsed: list[tuple[_GuideContributionCandidate, TopicContribution]] = []
    issues: list[GuideContributionError] = []
    for candidate in candidates:
        try:
            topic = parse_topic_contribution(_decoded_contribution(candidate.value), candidate.source)
            ownership = _ownership_error(candidate, topic)
            if ownership is not None:
                raise ownership
            parsed.append((candidate, topic))
        except GuideContributionError as error:
            if candidate.trusted:
                raise
            issues.append(error)

    grouped: dict[str, list[tuple[_GuideContributionCandidate, TopicContribution]]] = {}
    for item in parsed:
        grouped.setdefault(str(item[1].topic), []).append(item)
    retained: dict[str, tuple[_GuideContributionCandidate, TopicContribution]] = {}
    for slug in sorted(grouped):
        group = grouped[slug]
        trusted_group = [item for item in group if item[0].trusted]
        if len(trusted_group) > 1:
            sources = sorted(item[0].source for item in trusted_group)
            raise DuplicateTopicError(
                f"duplicate trusted guide topic {slug!r} from {', '.join(sources)}",
                source=sources[0],
                topic=slug,
                field_path="topic",
            )
        if trusted_group:
            retained[slug] = trusted_group[0]
            rejected = [item for item in group if not item[0].trusted]
        elif len(group) == 1:
            retained[slug] = group[0]
            rejected = []
        else:
            rejected = group
        for candidate, _topic in rejected:
            issues.append(
                DuplicateTopicError(
                    f"guide topic {slug!r} collides with another contribution",
                    source=candidate.source,
                    topic=slug,
                    field_path="topic",
                )
            )

    while True:
        names = frozenset(retained)
        invalid: list[tuple[str, _GuideContributionCandidate, str]] = []
        for slug, (candidate, topic) in retained.items():
            broken = sorted(str(link) for link in topic.related_topics if str(link) not in names)
            if broken:
                invalid.append((slug, candidate, broken[0]))
        if not invalid:
            break
        for slug, candidate, target in invalid:
            link_error = BrokenTopicLinkError(
                f"guide topic {slug!r} links to unknown topic {target!r}",
                source=candidate.source,
                topic=slug,
                field_path="related_topics",
            )
            if candidate.trusted:
                raise link_error
            issues.append(link_error)
            del retained[slug]

    topics = tuple(item[1] for _, item in sorted(retained.items()))
    return GuideCatalog(topics, tuple(GuideCatalogIssue(error) for error in sorted(issues, key=_issue_key)))
