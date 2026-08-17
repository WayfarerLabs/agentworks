"""Shared grammar for the two inert Markdown-shell directives."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from agentworks.guide.contract import GuideContentError

AGENT_OPEN = "<!-- agw:agent-only -->"
AGENT_CLOSE = "<!-- /agw:agent-only -->"

_DIRECTIVE_RE = re.compile(r"^<!--\s*(?P<body>/?agw:[\s\S]*?)\s*-->$")
_ATTRIBUTE_RE = re.compile(r'(?:^|[ \t]+)([a-z][a-z-]*)="([^"\n]*)"')
_INTEGER_RE = re.compile(r"[+-]?[0-9]+")
_INCLUDE_NAME = "agw:include"


def directive_body(line: str) -> str | None:
    """Return one exact column-zero Agentworks directive body, if present."""
    match = _DIRECTIVE_RE.fullmatch(line)
    return None if match is None else match.group("body")


def bounded_include_path(value: str) -> tuple[str, ...]:
    """Validate and split one package-relative Markdown include path."""
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or not value.endswith(".md")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise GuideContentError(f"guide include path {value!r} is not a bounded package Markdown path")
    return path.parts


def parse_include_directive(body: str, source: str) -> tuple[str, str, int]:
    """Parse the one supported expansion directive."""
    if not body.startswith(_INCLUDE_NAME):
        raise GuideContentError(f"guide shell {source!r} contains unknown directive {body!r}")
    attributes_text = body[len(_INCLUDE_NAME) :]
    attributes: dict[str, str] = {}
    cursor = 0
    for match in _ATTRIBUTE_RE.finditer(attributes_text):
        if attributes_text[cursor : match.start()].strip():
            raise GuideContentError(f"guide shell {source!r} contains a malformed include directive")
        key, value = match.groups()
        if key in attributes:
            raise GuideContentError(f"guide shell {source!r} repeats include attribute {key!r}")
        attributes[key] = value
        cursor = match.end()
    if attributes_text[cursor:].strip():
        raise GuideContentError(f"guide shell {source!r} contains a malformed include directive")
    unknown = set(attributes) - {"path", "heading", "heading-offset"}
    if unknown:
        raise GuideContentError(f"guide shell {source!r} has unknown include attribute {sorted(unknown)[0]!r}")
    missing = {"path", "heading"} - set(attributes)
    if missing:
        raise GuideContentError(f"guide shell {source!r} is missing include attribute {sorted(missing)[0]!r}")
    path = attributes["path"]
    heading = attributes["heading"]
    bounded_include_path(path)
    if not heading:
        raise GuideContentError(f"guide shell {source!r} has an empty include heading")
    raw_offset = attributes.get("heading-offset", "0")
    if _INTEGER_RE.fullmatch(raw_offset) is None:
        raise GuideContentError(f"guide shell {source!r} has a non-integer heading offset")
    return path, heading, int(raw_offset)
