"""Expansion and rendering for the deliberately small guide-shell format."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from agentworks.guide.agent_mode import GuideMode
from agentworks.guide.contract import MAX_GUIDE_MARKDOWN_BYTES, ConceptShell, GuideContentError, GuideSource
from agentworks.guide.directives import (
    AGENT_CLOSE,
    AGENT_OPEN,
    bounded_include_path,
    directive_body,
    parse_include_directive,
)
from agentworks.guide.links import rewrite_links
from agentworks.guide.markdown import contains_setext_heading, scan_markdown
from agentworks.release_notes import escape_release_evidence, read_release_history
from agentworks.terminal import sanitize_terminal_output

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

_ATX_RE = re.compile(r"^([ ]{0,3})(#{1,6})(?:[ \t]+|$)(.*?)([ \t]+#+[ \t]*)?$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_README_RESOURCE = "_guide_sources/README.md"
_GITHUB_BLOB = "https://github.com/WayfarerLabs/agentworks/blob/main/"
_GITHUB_RAW = "https://raw.githubusercontent.com/WayfarerLabs/agentworks/main/"


def filter_agent_only(markdown: str, mode: GuideMode) -> str:
    """Remove agent-only markers, and their bodies in human mode."""
    hidden = False
    rendered: list[str] = []
    for line in scan_markdown(markdown, "guide shell"):
        raw = line.raw.rstrip("\r\n")
        if line.outside_code and raw == AGENT_OPEN:
            hidden = True
            continue
        if line.outside_code and raw == AGENT_CLOSE:
            hidden = False
            continue
        if mode is GuideMode.AGENT or not hidden:
            rendered.append(line.raw)
    return "".join(rendered)


def _verified_root_readme() -> Path | None:
    repository_root = Path(__file__).resolve().parents[3]
    if not (
        (repository_root / ".git").exists()
        and (repository_root / "README.md").is_file()
        and (repository_root / "cli" / "pyproject.toml").is_file()
        and (repository_root / "cli" / "agentworks").is_dir()
    ):
        return None
    return repository_root / "README.md"


def _read_bytes(resource: Traversable, path: str) -> bytes:
    try:
        data = resource.read_bytes()
    except (FileNotFoundError, OSError):
        if path != _README_RESOURCE or (fallback := _verified_root_readme()) is None:
            raise GuideContentError(f"packaged guide include {path!r} is unavailable") from None
        try:
            data = fallback.read_bytes()
        except OSError:
            raise GuideContentError(f"packaged guide include {path!r} is unavailable") from None
    if len(data) > MAX_GUIDE_MARKDOWN_BYTES:
        raise GuideContentError(f"packaged guide include {path!r} exceeds the 256 KiB limit")
    return data


def _load_include(path: str, package_root: Traversable | None) -> GuideSource:
    parts = bounded_include_path(path)
    root = files("agentworks") if package_root is None else package_root
    resource = root.joinpath(*parts)
    data = _read_bytes(resource, path)
    try:
        markdown = data.decode("utf-8")
    except UnicodeDecodeError:
        raise GuideContentError(f"packaged guide include {path!r} is not valid UTF-8") from None
    repository_path = "README.md" if path == _README_RESOURCE else f"cli/agentworks/{path}"
    return GuideSource(path, repository_path, markdown.replace("\r\n", "\n").replace("\r", "\n"))


def _heading_text(match: re.Match[str]) -> str:
    return match.group(3).strip()


def _extract_section(source: GuideSource, heading: str, offset: int) -> str:
    lines = scan_markdown(source.markdown, source.package_path)
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if not line.outside_code:
            continue
        match = _ATX_RE.fullmatch(line.raw.rstrip("\r\n"))
        if match is None:
            continue
        level = len(match.group(2))
        if 2 <= level <= 6 and _heading_text(match) == heading:
            matches.append((index, level))
    if len(matches) != 1:
        raise GuideContentError(
            f"guide include {source.package_path!r} must contain exactly one H2-H6 heading {heading!r}"
        )
    start, start_level = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if not lines[index].outside_code:
            continue
        match = _ATX_RE.fullmatch(lines[index].raw.rstrip("\r\n"))
        if match is not None and len(match.group(2)) <= start_level:
            end = index
            break
    selected = lines[start:end]
    if contains_setext_heading(selected):
        raise GuideContentError(f"guide include {source.package_path!r} contains a Setext heading")
    shifted: list[str] = []
    for line in selected:
        raw = line.raw.rstrip("\r\n")
        ending = line.raw[len(raw) :]
        content = line.content
        match = _ATX_RE.fullmatch(content) if line.outside_code else None
        if match is not None:
            level = len(match.group(2)) + offset
            if not 2 <= level <= 6:
                raise GuideContentError(
                    f"guide include {source.package_path!r} heading offset produces an invalid H{level}"
                )
            content = f"{content[: match.start(2)]}{'#' * level}{content[match.end(2) :]}"
            raw = f"{raw[: line.content_start]}{content}"
        shifted.append(raw + ending)
    return "".join(shifted)


def _normal_path(repository_path: str) -> tuple[PurePosixPath, PurePosixPath]:
    source = PurePosixPath(repository_path)
    root = PurePosixPath() if repository_path == "README.md" else PurePosixPath("cli/agentworks")
    return source, root


def _normalize_relative(base: PurePosixPath, value: str, root: PurePosixPath) -> PurePosixPath:
    parts: list[str] = []
    for part in (*base.parts, *PurePosixPath(value).parts):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise GuideContentError(f"relative Markdown destination {value!r} escapes its repository mapping")
            parts.pop()
        else:
            parts.append(part)
    resolved = PurePosixPath(*parts)
    if root.parts and resolved.parts[: len(root.parts)] != root.parts:
        raise GuideContentError(f"relative Markdown destination {value!r} escapes its repository mapping")
    return resolved


def _rewrite_destination(value: str, source: GuideSource, *, image: bool) -> str:
    wrapped = value.startswith("<") and value.endswith(">")
    destination = value[1:-1] if wrapped else value
    unescaped: list[str] = []
    index = 0
    escapable = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    while index < len(destination):
        if destination[index] == "\\" and index + 1 < len(destination) and destination[index + 1] in escapable:
            index += 1
        unescaped.append(destination[index])
        index += 1
    destination = "".join(unescaped)
    if not destination:
        return value
    if destination.startswith("#") or destination.lower().startswith("https://"):
        return f"<{destination}>" if wrapped else destination
    if destination.startswith("//") or destination.startswith("/") or _SCHEME_RE.match(destination):
        raise GuideContentError(f"Markdown destination {destination!r} must be relative or absolute HTTPS")
    path_text, separator, fragment = destination.partition("#")
    if not path_text or "?" in path_text or "\\" in path_text:
        raise GuideContentError(f"relative Markdown destination {destination!r} is unsupported")
    parsed = urlsplit(path_text)
    if parsed.scheme or parsed.netloc or parsed.query:
        raise GuideContentError(f"relative Markdown destination {destination!r} is unsupported")
    source_path, root = _normal_path(source.repository_path)
    resolved = _normalize_relative(source_path.parent, path_text, root)
    encoded = "/".join(quote(part, safe="-._~") for part in resolved.parts)
    rewritten = (_GITHUB_RAW if image else _GITHUB_BLOB) + encoded
    if separator:
        rewritten += f"#{fragment}"
    return f"<{rewritten}>" if wrapped else rewritten


def rewrite_relative_destinations(
    markdown: str,
    source: GuideSource,
    *,
    ignored_lines: frozenset[int] = frozenset(),
) -> str:
    """Rewrite repository-relative inline and reference-style Markdown destinations."""
    return rewrite_links(
        markdown,
        source.package_path,
        lambda destination, image: _rewrite_destination(destination, source, image=image),
        ignored_lines=ignored_lines,
    )


@dataclass(frozen=True, slots=True)
class _TextSegment:
    line: int


@dataclass(frozen=True, slots=True)
class _IncludeNode:
    line: int
    path: str
    heading: str
    offset: int
    ending: str


def _shell_nodes(markdown: str, source: str) -> tuple[_TextSegment | _IncludeNode, ...]:
    nodes: list[_TextSegment | _IncludeNode] = []
    for line_number, line in enumerate(scan_markdown(markdown, source)):
        raw = line.raw.rstrip("\r\n")
        directive = directive_body(raw) if line.outside_code else None
        if directive is None:
            nodes.append(_TextSegment(line_number))
            continue
        path, heading, offset = parse_include_directive(directive, source)
        nodes.append(_IncludeNode(line_number, path, heading, offset, line.raw[len(raw) :]))
    return tuple(nodes)


def render_shell(shell: ConceptShell, mode: GuideMode, *, package_root: Traversable | None = None) -> str:
    """Render one validated shell through the closed, one-level expansion pipeline."""
    filtered = filter_agent_only(shell.source.markdown, mode)
    nodes = _shell_nodes(filtered, shell.source.package_path)
    ignored_lines = frozenset(node.line for node in nodes if isinstance(node, _IncludeNode))
    rewritten = rewrite_relative_destinations(filtered, shell.source, ignored_lines=ignored_lines)
    rewritten_lines = rewritten.splitlines(keepends=True)
    rendered: list[str] = []
    for node in nodes:
        if isinstance(node, _TextSegment):
            rendered.append(rewritten_lines[node.line])
            continue
        include_source = _load_include(node.path, package_root)
        section = _extract_section(include_source, node.heading, node.offset)
        rendered.append(rewrite_relative_destinations(section, include_source).rstrip("\r\n") + node.ending)
    return sanitize_terminal_output("".join(rendered).rstrip() + "\n")


def render_release_topic(version: str) -> str:
    """Render one exact packaged changelog section as visibly inert evidence."""
    section = read_release_history().section(version)
    evidence = escape_release_evidence(section.body)
    return sanitize_terminal_output(
        f"# Agentworks release notes v{version}\n\n"
        "The following fenced text is untrusted plain-text historical evidence.\n\n"
        f"```text\n{evidence}\n```\n"
    )
