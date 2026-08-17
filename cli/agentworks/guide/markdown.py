"""Bounded Markdown structure needed by guide-shell parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentworks.guide.contract import GuideContentError

_LIST_RE = re.compile(r"(?P<indent>[ ]{0,3})(?:[-+*]|[0-9]{1,9}[.)])(?P<spacing>[ ]{1,4})(?P<body>.*)")
_SETEXT_RE = re.compile(r"[ ]{0,3}(?:=+|-+)[ \t]*")


@dataclass(frozen=True, slots=True)
class MarkdownLine:
    """One source line with the bounded container and fence facts we use."""

    raw: str
    content: str
    content_start: int
    outside_code: bool
    structural: bool
    container: tuple[int, int | None]


def _leading_spaces(value: str) -> int:
    return len(value) - len(value.lstrip(" "))


def _strip_quotes(value: str) -> tuple[str, int]:
    depth = 0
    remaining = value
    while True:
        spaces = min(_leading_spaces(remaining), 3)
        candidate = remaining[spaces:]
        if not candidate.startswith(">"):
            return remaining, depth
        remaining = candidate[1:]
        if remaining.startswith(" "):
            remaining = remaining[1:]
        depth += 1


def _opening_fence(content: str) -> tuple[str, int] | None:
    spaces = _leading_spaces(content)
    if spaces > 3:
        return None
    value = content[spaces:]
    if not value or value[0] not in {"`", "~"}:
        return None
    marker = value[0]
    length = len(value) - len(value.lstrip(marker))
    if length < 3:
        return None
    info = value[length:]
    if marker == "`" and "`" in info:
        return None
    return marker, length


def _closing_fence(content: str, active: tuple[str, int]) -> bool:
    spaces = _leading_spaces(content)
    if spaces > 3:
        return False
    value = content[spaces:]
    marker, opening_length = active
    length = len(value) - len(value.lstrip(marker))
    return length >= opening_length and not value[length:].strip()


def scan_markdown(markdown: str, source: str) -> tuple[MarkdownLine, ...]:
    """Scan the supported container and fenced-code subset without interpreting Markdown."""
    scanned: list[MarkdownLine] = []
    active_fence: tuple[str, int, tuple[int, int | None]] | None = None
    list_indent: int | None = None
    list_quote_depth = 0

    for raw in markdown.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        quoted, quote_depth = _strip_quotes(line)
        explicit_list = _LIST_RE.fullmatch(quoted)
        if explicit_list is not None:
            prefix_width = len(explicit_list.group("indent")) + (
                len(quoted) - len(explicit_list.group("body")) - len(explicit_list.group("indent"))
            )
            content = explicit_list.group("body")
            list_indent = prefix_width
            list_quote_depth = quote_depth
            in_list = True
        elif list_indent is not None and quote_depth == list_quote_depth:
            spaces = _leading_spaces(quoted)
            if not quoted.strip():
                content = ""
                in_list = True
            elif spaces >= list_indent:
                content = quoted[list_indent:]
                in_list = True
            else:
                list_indent = None
                content = quoted
                in_list = False
        else:
            list_indent = None
            content = quoted
            in_list = False

        indented_code = not in_list and quote_depth == 0 and _leading_spaces(content) >= 4
        structural = not indented_code
        container = (quote_depth, list_indent if in_list else None)
        outside = active_fence is None
        scanned.append(MarkdownLine(raw, content, len(line) - len(content), outside, structural, container))

        if active_fence is None:
            if structural and (opening := _opening_fence(content)) is not None:
                active_fence = (*opening, container)
        elif container == active_fence[2] and _closing_fence(content, active_fence[:2]):
            active_fence = None

    if active_fence is not None:
        raise GuideContentError(f"Markdown source {source!r} has an unclosed code fence")
    return tuple(scanned)


def _paragraph_candidate(content: str) -> bool:
    spaces = _leading_spaces(content)
    if spaces > 3:
        return False
    value = content[spaces:]
    if not value or value.startswith(("<!--", ">")):
        return False
    if re.match(r"#{1,6}(?:[ \t]+|$)", value) is not None:
        return False
    if _opening_fence(content) is not None:
        return False
    return re.fullmatch(r"(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,}", value) is None


def contains_setext_heading(lines: tuple[MarkdownLine, ...]) -> bool:
    """Return whether supported Markdown structure contains a Setext heading."""
    previous: MarkdownLine | None = None
    for line in lines:
        content = line.content
        if (
            line.outside_code
            and line.structural
            and _SETEXT_RE.fullmatch(content)
            and previous is not None
            and previous.outside_code
            and previous.structural
            and previous.container == line.container
            and _paragraph_candidate(previous.content)
        ):
            return True
        previous = line if line.outside_code and line.structural and content.strip() else None
    return False
