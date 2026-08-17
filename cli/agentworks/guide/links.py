"""Bounded Markdown link recognition for guide source rewriting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.guide.contract import GuideContentError
from agentworks.guide.markdown import scan_markdown

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class _Definition:
    line: int
    start: int
    end: int
    destination: str


@dataclass(frozen=True, slots=True)
class _Use:
    label: str
    image: bool


def _escaped(value: str, index: int) -> bool:
    slashes = 0
    cursor = index - 1
    while cursor >= 0 and value[cursor] == "\\":
        slashes += 1
        cursor -= 1
    return slashes % 2 == 1


def _closing_bracket(value: str, opening: int) -> int | None:
    depth = 0
    for index in range(opening + 1, len(value)):
        if _escaped(value, index):
            continue
        if value[index] == "[":
            depth += 1
        elif value[index] == "]":
            if depth == 0:
                return index
            depth -= 1
    return None


def _unescape(value: str) -> str:
    rendered: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            index += 1
        rendered.append(value[index])
        index += 1
    return "".join(rendered)


def _label(value: str) -> str:
    return " ".join(_unescape(value).split()).casefold()


def _code_mask(value: str) -> tuple[bool, ...]:
    masked = [False] * len(value)
    index = 0
    while index < len(value):
        if value[index] != "`" or _escaped(value, index):
            index += 1
            continue
        length = len(value[index:]) - len(value[index:].lstrip("`"))
        delimiter = "`" * length
        closing = value.find(delimiter, index + length)
        if closing < 0:
            index += length
            continue
        for position in range(index, closing + length):
            masked[position] = True
        index = closing + length
    return tuple(masked)


def _destination(value: str, start: int, *, inline: bool) -> tuple[int, int, int] | None:
    cursor = start
    while cursor < len(value) and value[cursor] in " \t":
        cursor += 1
    destination_start = cursor
    if inline and cursor > start and cursor < len(value) and value[cursor] in {'"', "'", "("}:
        destination_end = cursor
    elif cursor < len(value) and value[cursor] == "<":
        cursor += 1
        destination_start = cursor
        while cursor < len(value) and (value[cursor] != ">" or _escaped(value, cursor)):
            cursor += 1
        if cursor >= len(value):
            return None
        destination_end = cursor
        cursor += 1
    else:
        depth = 0
        while cursor < len(value):
            character = value[cursor]
            if _escaped(value, cursor):
                cursor += 1
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                if inline and depth == 0:
                    break
                if depth == 0:
                    return None
                depth -= 1
            elif character in " \t":
                break
            cursor += 1
        if depth:
            return None
        destination_end = cursor

    while cursor < len(value) and value[cursor] in " \t":
        cursor += 1
    if inline and cursor < len(value) and value[cursor] == ")":
        return destination_start, destination_end, cursor + 1
    if not inline and cursor == len(value):
        return destination_start, destination_end, cursor
    if cursor >= len(value) or value[cursor] not in {'"', "'", "("}:
        return None
    opening = value[cursor]
    closing = ")" if opening == "(" else opening
    cursor += 1
    depth = 0
    while cursor < len(value):
        if _escaped(value, cursor):
            cursor += 2
            continue
        character = value[cursor]
        if opening == "(" and character == "(":
            depth += 1
        elif character == closing:
            if depth == 0:
                cursor += 1
                break
            depth -= 1
        cursor += 1
    else:
        return None
    while cursor < len(value) and value[cursor] in " \t":
        cursor += 1
    if inline:
        if cursor < len(value) and value[cursor] == ")":
            return destination_start, destination_end, cursor + 1
        return None
    return (destination_start, destination_end, cursor) if cursor == len(value) else None


def _definition(content: str, offset: int, line: int) -> tuple[str, _Definition] | None:
    spaces = len(content) - len(content.lstrip(" "))
    if spaces > 3 or spaces >= len(content) or content[spaces] != "[":
        return None
    closing = _closing_bracket(content, spaces)
    if closing is None or closing + 1 >= len(content) or content[closing + 1] != ":":
        return None
    start = closing + 2
    if start >= len(content) or content[start] not in " \t":
        return None
    parsed = _destination(content, start, inline=False)
    if parsed is None:
        return None
    destination_start, destination_end, _end = parsed
    label = _label(content[spaces + 1 : closing])
    if not label:
        return None
    return label, _Definition(
        line,
        offset + destination_start,
        offset + destination_end,
        content[destination_start:destination_end],
    )


def _uses(value: str, definitions: set[str]) -> tuple[tuple[_Use, ...], tuple[tuple[int, int, bool], ...]]:
    references: list[_Use] = []
    inline: list[tuple[int, int, bool]] = []
    masked = _code_mask(value)
    index = 0
    while index < len(value):
        image = (
            value[index] == "!" and index + 1 < len(value) and value[index + 1] == "[" and not _escaped(value, index)
        )
        opening = index + 1 if image else index
        if opening >= len(value) or value[opening] != "[" or _escaped(value, opening) or masked[opening]:
            index += 1
            continue
        closing = _closing_bracket(value, opening)
        if closing is None:
            index += 1
            continue
        after = closing + 1
        if after < len(value) and value[after] == "(":
            parsed = _destination(value, after + 1, inline=True)
            if parsed is not None:
                destination_start, destination_end, link_end = parsed
                inline.append((destination_start, destination_end, image))
                index = link_end
                continue
        text_label = _label(value[opening + 1 : closing])
        if after < len(value) and value[after] == "[" and not _escaped(value, after):
            label_end = _closing_bracket(value, after)
            if label_end is not None:
                explicit = value[after + 1 : label_end]
                label = text_label if not explicit else _label(explicit)
                if label not in definitions:
                    raise GuideContentError(f"Markdown contains a missing reference definition {label!r}")
                references.append(_Use(label, image))
                index = label_end + 1
                continue
        if text_label in definitions:
            references.append(_Use(text_label, image))
        index = closing + 1
    return tuple(references), tuple(inline)


def rewrite_links(
    markdown: str,
    source: str,
    rewrite: Callable[[str, bool], str],
) -> str:
    """Rewrite recognized Markdown destinations while preserving all other source bytes."""
    lines = scan_markdown(markdown, source)
    definitions: dict[str, _Definition] = {}
    definition_lines: set[int] = set()
    for index, line in enumerate(lines):
        if not line.outside_code or not line.structural:
            continue
        parsed = _definition(line.content, line.content_start, index)
        if parsed is None:
            continue
        label, definition = parsed
        if label in definitions:
            raise GuideContentError(f"Markdown source {source!r} repeats reference definition {label!r}")
        definitions[label] = definition
        definition_lines.add(index)

    reference_kinds: dict[str, set[bool]] = {}
    inline_by_line: dict[int, tuple[tuple[int, int, bool], ...]] = {}
    for index, line in enumerate(lines):
        if not line.outside_code or not line.structural or index in definition_lines:
            continue
        uses, inline = _uses(line.raw.rstrip("\r\n"), set(definitions))
        for use in uses:
            reference_kinds.setdefault(use.label, set()).add(use.image)
        if inline:
            inline_by_line[index] = inline

    edits: dict[int, list[tuple[int, int, str]]] = {}
    for label, kinds in reference_kinds.items():
        definition = definitions[label]
        link_target = rewrite(definition.destination, False)
        image_target = rewrite(definition.destination, True)
        if kinds == {False, True} and link_target != image_target:
            raise GuideContentError(f"Markdown reference {label!r} in {source!r} is shared by a link and image")
        replacement = image_target if True in kinds else link_target
        edits.setdefault(definition.line, []).append((definition.start, definition.end, replacement))
    for line_index, inline in inline_by_line.items():
        raw = lines[line_index].raw.rstrip("\r\n")
        for start, end, image in inline:
            edits.setdefault(line_index, []).append((start, end, rewrite(raw[start:end], image)))

    rendered: list[str] = []
    for index, line in enumerate(lines):
        value = line.raw
        for start, end, replacement in sorted(edits.get(index, ()), reverse=True):
            value = value[:start] + replacement + value[end:]
        rendered.append(value)
    return "".join(rendered)
