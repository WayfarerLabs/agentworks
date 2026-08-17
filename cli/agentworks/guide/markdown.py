"""Bounded Markdown structure needed by guide-shell parsing.

Container facts remain explicit so imported Setext and ATX headings can be detected and shifted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from agentworks.guide.contract import GuideContentError

_LIST_RE = re.compile(r"(?P<indent>[ ]{0,3})(?:[-+*]|[0-9]{1,9}[.)])(?P<spacing>[ ]+)(?P<body>.*)")
_SETEXT_RE = re.compile(r"[ ]{0,3}(?:=+|-+)[ \t]*")
type _Container = Literal["quote"] | tuple[Literal["list"], int, int]
type _ContainerStack = tuple[_Container, ...]
_QUOTE: _Container = "quote"


@dataclass(frozen=True, slots=True)
class MarkdownLine:
    """One source line with the bounded container and fence facts we use."""

    raw: str
    content: str
    content_start: int
    outside_code: bool
    structural: bool
    container: _ContainerStack


def _leading_spaces(value: str) -> int:
    return len(value) - len(value.lstrip(" "))


def _quote_body(value: str) -> str | None:
    spaces = min(_leading_spaces(value), 3)
    candidate = value[spaces:]
    if not candidate.startswith(">"):
        return None
    remaining = candidate[1:]
    return remaining[1:] if remaining.startswith(" ") else remaining


def _list_body(value: str) -> tuple[str, int] | None:
    match = _LIST_RE.fullmatch(value)
    if match is None:
        return None
    spacing = match.group("spacing")
    body = match.group("body")
    if len(spacing) > 4:
        body = f"{spacing[1:]}{body}"
    return body, len(value) - len(body)


def _has_leading_tab(value: str) -> bool:
    prefix_length = len(value) - len(value.lstrip(" \t"))
    return "\t" in value[:prefix_length]


def _strip_containers(
    value: str,
    active: _ContainerStack,
    next_list_id: int,
) -> tuple[str, _ContainerStack, int]:
    containers: list[_Container] = []
    remaining = value
    active_index = 0
    following_active = True
    while True:
        if following_active and active_index < len(active):
            active_container = active[active_index]
            if active_container == _QUOTE:
                if (body := _quote_body(remaining)) is not None:
                    containers.append(_QUOTE)
                    remaining = body
                    active_index += 1
                    continue
            else:
                assert isinstance(active_container, tuple)
                if _leading_spaces(remaining) >= active_container[1]:
                    containers.append(active_container)
                    indent = active_container[1]
                    remaining = remaining[indent:]
                    active_index += 1
                    continue
            if not remaining.strip():
                for container in active[active_index:]:
                    if container == _QUOTE:
                        break
                    containers.append(container)
                remaining = ""
                break
            following_active = False

        if (body := _quote_body(remaining)) is not None:
            containers.append(_QUOTE)
            remaining = body
            continue
        if (listed := _list_body(remaining)) is not None:
            remaining, indent = listed
            containers.append(("list", indent, next_list_id))
            next_list_id += 1
            continue
        break
    return remaining, tuple(containers), next_list_id


def _follow_containers(value: str, expected: _ContainerStack) -> str | None:
    remaining = value
    for index, container in enumerate(expected):
        if container == _QUOTE:
            if (body := _quote_body(remaining)) is None:
                return None
            remaining = body
            continue
        assert isinstance(container, tuple)
        if _leading_spaces(remaining) >= container[1]:
            remaining = remaining[container[1] :]
        elif not remaining.strip() and all(item != _QUOTE for item in expected[index:]):
            return ""
        else:
            return None
    return remaining


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
    """Scan the supported tab-free container and fenced-code subset."""
    scanned: list[MarkdownLine] = []
    active_fence: tuple[str, int, _ContainerStack] | None = None
    active_containers: _ContainerStack = ()
    next_list_id = 0

    for raw in markdown.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        if active_fence is not None:
            content = _follow_containers(line, active_fence[2])
            if content is not None:
                scanned.append(MarkdownLine(raw, content, len(line) - len(content), False, True, active_fence[2]))
                if _closing_fence(content, active_fence[:2]):
                    active_fence = None
                continue
            active_fence = None

        if _has_leading_tab(line):
            raise GuideContentError(f"Markdown source {source!r} uses unsupported leading tab indentation")
        content, container, next_list_id = _strip_containers(line, active_containers, next_list_id)
        if _has_leading_tab(content):
            raise GuideContentError(f"Markdown source {source!r} uses unsupported leading tab indentation")
        active_containers = container
        indented_code = _leading_spaces(content) >= 4
        structural = not indented_code
        scanned.append(MarkdownLine(raw, content, len(line) - len(content), True, structural, container))
        if structural and (opening := _opening_fence(content)) is not None:
            active_fence = (*opening, container)

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
