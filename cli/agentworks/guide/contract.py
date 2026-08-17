"""Small structural contract for package-owned Markdown guide shells."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from agentworks.errors import NotFoundError, ValidationError

MAX_GUIDE_MARKDOWN_BYTES = 256 * 1024

_CONCEPT_RE = re.compile(r"^concept-[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SHELL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


class GuideContentError(ValidationError):
    """A first-party packaged guide document violates the shell contract."""


class UnknownGuideTopicError(NotFoundError):
    """A requested guide topic is not installed."""

    def __init__(self, topic: str, names: tuple[str, ...]) -> None:
        super().__init__(f"unknown guide topic {topic!r}", entity_kind="guide-topic", entity_name=topic)
        self.topic = topic
        self.close_matches = tuple(difflib.get_close_matches(topic, names, n=3))


@dataclass(frozen=True, slots=True)
class GuideSource:
    """One Markdown document and its repository-relative identity."""

    package_path: str
    repository_path: str
    markdown: str


@dataclass(frozen=True, slots=True)
class ConceptShell:
    """One discovered concept shell."""

    slug: str
    title: str
    description: str
    index_order: int | None
    source: GuideSource


@dataclass(frozen=True, slots=True)
class IndexShell:
    """The reserved, non-addressable guide index shell."""

    title: str
    description: str
    source: GuideSource


def shell_slug(name: str) -> str:
    """Map one strict lower-kebab filename stem to its concept slug."""
    if _SHELL_NAME_RE.fullmatch(name) is None:
        raise GuideContentError(f"guide shell filename {name!r} must use lower kebab case")
    return f"concept-{name}"


def is_concept_topic(value: str) -> bool:
    """Return whether a value has the ordinary concept-topic shape."""
    return _CONCEPT_RE.fullmatch(value) is not None
