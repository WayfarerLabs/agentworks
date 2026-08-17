"""Deterministic discovery of package-owned Markdown concept shells."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from agentworks.guide.contract import (
    MAX_GUIDE_MARKDOWN_BYTES,
    ConceptShell,
    GuideContentError,
    GuideSource,
    IndexShell,
    shell_slug,
)
from agentworks.guide.directives import AGENT_CLOSE, AGENT_OPEN, directive_body, parse_include_directive
from agentworks.guide.markdown import contains_setext_heading, scan_markdown

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

_FRONTMATTER_RE = re.compile(
    r"\A---\ndescription:[ \t]+(?P<description>[^\n]+)\n"
    r"(?:index-order:[ \t]+(?P<index_order>[0-9]{1,4})\n)?---\n(?P<body>.*)\Z",
    re.DOTALL,
)
_ATX_RE = re.compile(r"^[ ]{0,3}(#{1,6})(?:[ \t]+|$)(.*?)(?:[ \t]+#+[ \t]*)?$")
CORE_INDEX_PATH = "guide/guide-content/_index.md"


@dataclass(frozen=True, slots=True)
class GuideCatalog:
    """The complete first-party concept-shell catalog."""

    index: IndexShell
    topics: tuple[ConceptShell, ...]

    def names(self) -> tuple[str, ...]:
        return tuple(topic.slug for topic in self.topics)

    def lookup(self, slug: str) -> ConceptShell | None:
        return next((topic for topic in self.topics if topic.slug == slug), None)

    def indexed_topics(self) -> tuple[ConceptShell, ...]:
        """Return concise-index concepts in their deterministic display order."""
        return tuple(
            sorted(
                (topic for topic in self.topics if topic.index_order is not None),
                key=lambda topic: (topic.index_order, topic.slug),
            )
        )


def _read_markdown(resource: Traversable, package_path: str) -> str:
    try:
        data = resource.read_bytes()
    except OSError as error:
        raise GuideContentError(f"cannot read packaged guide document {package_path!r}: {error}") from None
    if len(data) > MAX_GUIDE_MARKDOWN_BYTES:
        raise GuideContentError(f"packaged guide document {package_path!r} exceeds the 256 KiB limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise GuideContentError(f"packaged guide document {package_path!r} is not valid UTF-8") from None
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _structural_shell(body: str, package_path: str) -> str:
    h1_titles: list[str] = []
    agent_only = False
    lines = scan_markdown(body, package_path)
    if contains_setext_heading(lines):
        raise GuideContentError(f"guide shell {package_path!r} contains a Setext heading")
    for scanned in lines:
        line = scanned.raw.rstrip("\r\n")
        if not scanned.outside_code:
            continue
        if line == AGENT_OPEN:
            if agent_only:
                raise GuideContentError(f"guide shell {package_path!r} nests an agent-only fence")
            agent_only = True
            continue
        if line == AGENT_CLOSE:
            if not agent_only:
                raise GuideContentError(f"guide shell {package_path!r} closes an unopened agent-only fence")
            agent_only = False
            continue
        if (directive := directive_body(line)) is not None:
            parse_include_directive(directive, package_path)
            continue
        heading = _ATX_RE.fullmatch(line)
        if heading is not None and len(heading.group(1)) == 1 and not agent_only:
            title = heading.group(2).strip()
            if title:
                h1_titles.append(title)
    if agent_only:
        raise GuideContentError(f"guide shell {package_path!r} has an unclosed agent-only fence")
    if len(h1_titles) != 1:
        raise GuideContentError(
            f"guide shell {package_path!r} must contain exactly one level-one ATX heading outside agent-only fences"
        )
    return h1_titles[0]


def _parse_shell(resource: Traversable, package_path: str) -> ConceptShell | IndexShell:
    text = _read_markdown(resource, package_path)
    match = _FRONTMATTER_RE.fullmatch(text)
    if match is None:
        raise GuideContentError(
            f"guide shell {package_path!r} must have ordered description and optional index-order frontmatter "
            "followed by a Markdown body"
        )
    description = match.group("description").strip()
    if not description:
        raise GuideContentError(f"guide shell {package_path!r} has an empty description")
    index_order_text = match.group("index_order")
    index_order = int(index_order_text) if index_order_text is not None else None
    body = match.group("body")
    title = _structural_shell(body, package_path)
    filename = PurePosixPath(package_path).name
    source = GuideSource(package_path, f"cli/agentworks/{package_path}", body)
    if package_path == CORE_INDEX_PATH:
        if index_order is not None:
            raise GuideContentError(f"reserved guide index {package_path!r} cannot have index-order")
        return IndexShell(title, description, source)
    if filename.startswith("_"):
        raise GuideContentError(f"guide shell filename {filename!r} is reserved")
    slug = shell_slug(filename.removesuffix(".md"))
    return ConceptShell(
        slug,
        title,
        description,
        index_order,
        source,
    )


def _shell_resources(root: Traversable) -> tuple[tuple[str, Traversable], ...]:
    found: list[tuple[str, Traversable]] = []

    def walk(directory: Traversable, relative: PurePosixPath) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as error:
            raise GuideContentError(f"cannot inspect packaged guide directory {str(relative)!r}: {error}") from None
        for child in children:
            child_relative = relative / child.name
            if child.is_dir():
                walk(child, child_relative)
            elif relative.name == "guide-content" and child.name.endswith(".md"):
                found.append((child_relative.as_posix(), child))

    walk(root, PurePosixPath())
    return tuple(found)


def discover_concept_shells(package_root: Traversable | None = None) -> GuideCatalog:
    """Discover and validate all direct Markdown children of first-party guide-content directories."""
    root = files("agentworks") if package_root is None else package_root
    shells = tuple(_parse_shell(resource, path) for path, resource in _shell_resources(root))
    indexes = tuple(shell for shell in shells if isinstance(shell, IndexShell))
    if len(indexes) != 1:
        raise GuideContentError(f"required reserved guide index {CORE_INDEX_PATH!r} is missing")
    topics = tuple(shell for shell in shells if isinstance(shell, ConceptShell))
    by_slug: dict[str, list[ConceptShell]] = {}
    for topic in topics:
        by_slug.setdefault(topic.slug, []).append(topic)
    collisions = tuple((slug, values) for slug, values in sorted(by_slug.items()) if len(values) > 1)
    if collisions:
        slug, values = collisions[0]
        paths = ", ".join(topic.source.package_path for topic in values)
        raise GuideContentError(f"duplicate guide topic {slug!r} from {paths}")
    return GuideCatalog(indexes[0], tuple(sorted(topics, key=lambda topic: topic.source.package_path)))
