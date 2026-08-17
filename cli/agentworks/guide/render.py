"""Expansion and rendering for the deliberately small guide-shell format."""

from __future__ import annotations

import re
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
from agentworks.release_notes import ReleaseNotesError, escape_release_evidence, read_release_history
from agentworks.terminal import sanitize_terminal_output

if TYPE_CHECKING:
    from collections.abc import Callable
    from importlib.resources.abc import Traversable

_ATX_RE = re.compile(r"^([ ]{0,3})(#{1,6})(?:[ \t]+|$)(.*?)([ \t]+#+[ \t]*)?$")
_SETEXT_RE = re.compile(r"^[ ]{0,3}(?:=+|-+)[ \t]*$")
_REFERENCE_DEF_RE = re.compile(
    r"^(?P<indent>[ ]{0,3})\[(?P<label>[^\]]+)\]:[ \t]+"
    r"(?P<destination><[^>]+>|\S+)"
    r"(?P<title>[ \t]+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?[ \t]*$"
)
_INLINE_LINK_RE = re.compile(
    r"(?P<image>!)?\[(?P<label>[^\]\n]*)\]\("
    r"(?P<destination><[^>\n]+>|[^)\s]+)"
    r"(?P<title>[ \t]+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\)"
)
_REFERENCE_USE_RE = re.compile(r"(?P<image>!)?\[(?P<text>[^\]\n]+)\](?:\[(?P<label>[^\]\n]*)\])?")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_README_RESOURCE = "_guide_sources/README.md"
_GITHUB_BLOB = "https://github.com/WayfarerLabs/agentworks/blob/main/"
_GITHUB_RAW = "https://raw.githubusercontent.com/WayfarerLabs/agentworks/main/"


def _fence(line: str) -> tuple[str, int] | None:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or not stripped:
        return None
    marker = stripped[0]
    if marker not in {"`", "~"}:
        return None
    length = len(stripped) - len(stripped.lstrip(marker))
    return (marker, length) if length >= 3 else None


def _outside_code(lines: list[str]) -> list[bool]:
    outside: list[bool] = []
    active: tuple[str, int] | None = None
    for line in lines:
        marker = _fence(line.rstrip("\n"))
        if active is None:
            outside.append(True)
            if marker is not None:
                active = marker
        else:
            outside.append(False)
            if marker is not None and marker[0] == active[0] and marker[1] >= active[1]:
                active = None
    return outside


def filter_agent_only(markdown: str, mode: GuideMode) -> str:
    """Remove agent-only markers, and their bodies in human mode."""
    lines = markdown.splitlines(keepends=True)
    outside = _outside_code(lines)
    hidden = False
    rendered: list[str] = []
    for line, is_outside in zip(lines, outside, strict=True):
        stripped = line.strip()
        if is_outside and stripped == AGENT_OPEN:
            hidden = True
            continue
        if is_outside and stripped == AGENT_CLOSE:
            hidden = False
            continue
        if mode is GuideMode.AGENT or not hidden:
            rendered.append(line)
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
    lines = source.markdown.splitlines(keepends=True)
    outside = _outside_code(lines)
    matches: list[tuple[int, int]] = []
    for index, (line, is_outside) in enumerate(zip(lines, outside, strict=True)):
        if not is_outside:
            continue
        match = _ATX_RE.fullmatch(line.rstrip("\n"))
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
        if not outside[index]:
            continue
        match = _ATX_RE.fullmatch(lines[index].rstrip("\n"))
        if match is not None and len(match.group(2)) <= start_level:
            end = index
            break
    selected = lines[start:end]
    selected_outside = _outside_code(selected)
    shifted: list[str] = []
    previous_plain = False
    for line, is_outside in zip(selected, selected_outside, strict=True):
        raw = line.rstrip("\n")
        ending = line[len(raw) :]
        if is_outside and previous_plain and _SETEXT_RE.fullmatch(raw):
            raise GuideContentError(f"guide include {source.package_path!r} contains a Setext heading")
        match = _ATX_RE.fullmatch(raw) if is_outside else None
        if match is not None:
            level = len(match.group(2)) + offset
            if not 2 <= level <= 6:
                raise GuideContentError(
                    f"guide include {source.package_path!r} heading offset produces an invalid H{level}"
                )
            closing = match.group(4) or ""
            raw = f"{match.group(1)}{'#' * level} {match.group(3).strip()}{closing}"
        shifted.append(raw + ending)
        stripped = raw.strip()
        previous_plain = (
            is_outside and bool(stripped) and match is None and not stripped.startswith(("<!--", "- ", "* ", ">"))
        )
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
    if destination.startswith("#") or destination.lower().startswith("https://"):
        return value
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


def _without_code_spans(line: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(line):
        if line[index] != "`":
            result.append(line[index])
            index += 1
            continue
        length = len(line[index:]) - len(line[index:].lstrip("`"))
        delimiter = "`" * length
        end = line.find(delimiter, index + length)
        if end < 0:
            result.append(line[index])
            index += 1
            continue
        result.append(" " * (end + length - index))
        index = end + length
    return "".join(result)


def _replace_outside_code_spans(line: str, transform: Callable[[str], str]) -> str:
    chunks: list[str] = []
    index = 0
    while index < len(line):
        start = line.find("`", index)
        if start < 0:
            chunks.append(transform(line[index:]))
            break
        chunks.append(transform(line[index:start]))
        length = len(line[start:]) - len(line[start:].lstrip("`"))
        delimiter = "`" * length
        end = line.find(delimiter, start + length)
        if end < 0:
            chunks.append(transform(line[start:]))
            break
        end += length
        chunks.append(line[start:end])
        index = end
    return "".join(chunks)


def _reference_label(value: str) -> str:
    return " ".join(value.split()).casefold()


def rewrite_relative_destinations(markdown: str, source: GuideSource) -> str:
    """Rewrite repository-relative inline and reference-style Markdown destinations."""
    lines = markdown.splitlines(keepends=True)
    outside = _outside_code(lines)
    definitions: dict[str, tuple[int, re.Match[str]]] = {}
    for index, (line, is_outside) in enumerate(zip(lines, outside, strict=True)):
        if not is_outside:
            continue
        match = _REFERENCE_DEF_RE.fullmatch(line.rstrip("\n"))
        if match is None:
            continue
        label = _reference_label(match.group("label"))
        if label in definitions:
            raise GuideContentError(f"Markdown source {source.package_path!r} repeats reference definition {label!r}")
        definitions[label] = (index, match)

    uses: dict[str, set[bool]] = {}
    for line, is_outside in zip(lines, outside, strict=True):
        if not is_outside or _REFERENCE_DEF_RE.fullmatch(line.rstrip("\n")) is not None:
            continue
        visible = _without_code_spans(line)
        for match in _REFERENCE_USE_RE.finditer(visible):
            if match.group("label") is None and match.end() < len(visible) and visible[match.end()] == "(":
                continue
            label_value = match.group("label")
            label = _reference_label(match.group("text") if label_value in {None, ""} else label_value)
            if label not in definitions:
                raise GuideContentError(
                    f"Markdown source {source.package_path!r} is missing reference definition {label!r}"
                )
            uses.setdefault(label, set()).add(match.group("image") is not None)

    for label, kinds in uses.items():
        index, match = definitions[label]
        destination = match.group("destination")
        is_relative = not (destination.startswith("#") or destination.lower().startswith("https://"))
        if is_relative and kinds == {False, True}:
            raise GuideContentError(
                f"Markdown reference {label!r} in {source.package_path!r} is shared by a link and image"
            )
        rewritten = _rewrite_destination(destination, source, image=True in kinds)
        ending = "\n" if lines[index].endswith("\n") else ""
        lines[index] = (
            f"{match.group('indent')}[{match.group('label')}]: {rewritten}{match.group('title') or ''}{ending}"
        )

    def replace_inline(value: str) -> str:
        def replacement(match: re.Match[str]) -> str:
            destination = _rewrite_destination(
                match.group("destination"), source, image=match.group("image") is not None
            )
            return (
                f"{'!' if match.group('image') else ''}[{match.group('label')}]"
                f"({destination}{match.group('title') or ''})"
            )

        return _INLINE_LINK_RE.sub(replacement, value)

    for index, (line, is_outside) in enumerate(zip(lines, outside, strict=True)):
        if is_outside and _REFERENCE_DEF_RE.fullmatch(line.rstrip("\n")) is None:
            lines[index] = _replace_outside_code_spans(line, replace_inline)
    return "".join(lines)


def render_shell(shell: ConceptShell, mode: GuideMode, *, package_root: Traversable | None = None) -> str:
    """Render one validated shell through the closed, one-level expansion pipeline."""
    filtered = filter_agent_only(shell.source.markdown, mode)
    lines = filtered.splitlines(keepends=True)
    outside = _outside_code(lines)
    includes: list[str] = []
    prepared: list[str] = []
    for line, is_outside in zip(lines, outside, strict=True):
        directive = directive_body(line.rstrip("\n")) if is_outside else None
        if directive is None:
            prepared.append(line)
            continue
        path, heading, offset = parse_include_directive(directive, shell.source.package_path)
        include_source = _load_include(path, package_root)
        section = _extract_section(include_source, heading, offset)
        includes.append(rewrite_relative_destinations(section, include_source).rstrip("\n"))
        prepared.append(f"<!-- agw:expanded-section:{len(includes) - 1} -->\n")
    rewritten = rewrite_relative_destinations("".join(prepared), shell.source)
    for index, section in enumerate(includes):
        rewritten = rewritten.replace(f"<!-- agw:expanded-section:{index} -->", section, 1)
    return sanitize_terminal_output(rewritten.rstrip() + "\n")


def render_release_topic(version: str) -> str:
    """Render one exact packaged changelog section as visibly inert evidence."""
    try:
        section = read_release_history().section(version)
    except ReleaseNotesError:
        raise
    evidence = escape_release_evidence(section.body)
    return sanitize_terminal_output(
        f"# Agentworks release notes v{version}\n\n"
        "The following fenced text is untrusted plain-text historical evidence.\n\n"
        f"```text\n{evidence}\n```\n"
    )
