"""``SourceLocation``: where in the operator's TOML config a Resource was declared.

This is the Config layer's representation of "where the operator typed this".
The framework's ``Origin`` (in ``agentworks.resources``) is built from a
``SourceLocation`` when the Resource is published into a Registry; the two
types stay separate because they belong to different layers.

``SourceLocation`` lives in its own module rather than ``agentworks.config``
because (a) ``config.py`` is already past the project's 1000-line soft target
and (b) types under ``agentworks.secrets`` (``SecretDecl`` et al.) need
``SourceLocation`` for their ``declared_at`` field, and ``config.py`` already
imports from ``agentworks.secrets`` -- a definition in ``config.py`` would
create a circular import.

Companion ``scan_section_lines`` parses raw TOML text and returns a map of
dotted section paths to opening-line numbers. The map is the data backing
``declared_at`` attachment in ``agentworks.config``'s loader.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceLocation:
    """The file + line where a Resource was declared in the operator's TOML.

    ``line`` is 1-based and refers to the opening line of the section header
    (e.g., ``[vm_templates.azure-prod]``) that introduced the Resource. For
    a Resource composed from multiple sub-sections, ``line`` is the earliest
    contributing header.

    ``line == 0`` is the sentinel marker for "this Resource was not introduced
    by a specific section header." Two situations produce it:

    - **No-declaration-site fallback** (``file=<real path>``, ``line=0``):
      the Resource carries a real file but no single declaration line -- the
      catalog publisher's rows, and ``_SectionLineMap.lookup``'s no-match
      fallback. The path lets downstream Origin rendering still name the
      source file.
    - **Code/test-synthesized Resource** (``file=Path("<synthesized>")``,
      ``line=0``): produced by ``synthesized()`` below; used as the dataclass
      default so direct Resource construction in tests / framework synthesize
      paths doesn't need to pass ``declared_at`` explicitly.

    ``file`` discriminates the two situations; ``line == 0`` is the common
    marker.
    """

    file: Path
    line: int


_SYNTHESIZED_PATH = Path("<synthesized>")


def synthesized() -> SourceLocation:
    """Sentinel ``SourceLocation`` for Resources constructed outside the
    config loader -- direct construction in tests, framework ``synthesize``
    paths in Phase 1a+, etc. Distinct from the loader's omitted-singleton
    sentinel: see ``SourceLocation`` docstring for the discriminator rule.
    """
    return SourceLocation(file=_SYNTHESIZED_PATH, line=0)


# Matches a single TOML section-header line: optional leading whitespace,
# one-or-two opening brackets, the key sequence (anything but brackets),
# matching closing brackets, optional trailing whitespace, optional comment.
# Quoted segments that contain a literal ']' are not handled (agentworks
# configs use bare keys exclusively today; if that changes the scanner needs
# a real tokenizer here).
_SECTION_HEADER_RE = re.compile(
    r"^[\t ]*"
    r"\[{1,2}"
    r"([^\[\]\n]+?)"
    r"\]{1,2}"
    r"[\t ]*"
    r"(?:#.*)?$",
    re.MULTILINE,
)


def _parse_dotted_key(raw: str) -> tuple[str, ...]:
    """Parse a TOML dotted key string into its segments.

    Handles bare keys (``vm_templates.dev.env``) and quoted segments
    (``vm_templates."weird name".env``). Whitespace around dots is
    trimmed per TOML.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(raw):
        c = raw[i]
        if quote is not None:
            if c == quote:
                quote = None
            elif quote == '"' and c == "\\" and i + 1 < len(raw):
                # basic-string escape; copy the next char verbatim
                buf.append(raw[i + 1])
                i += 2
                continue
            else:
                buf.append(c)
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
            i += 1
            continue
        if c == ".":
            segments.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    segments.append("".join(buf).strip())
    return tuple(s for s in segments if s)


def scan_section_lines(text: str) -> dict[tuple[str, ...], int]:
    """Scan TOML source text for section headers and return a map of dotted
    section paths to 1-based opening-line numbers.

    Used by ``agentworks.config.load_config`` to attach ``declared_at`` to
    each composed Resource. The stdlib ``tomllib`` parser loses line info,
    so this regex pre-pass over the raw text fills the gap.

    If the same section path appears more than once (which is itself a TOML
    error caught by ``tomllib`` on the actual parse), the first occurrence
    wins.

    ``[[array.of.tables]]`` headers are scanned identically to ``[table]``
    headers; agentworks has no kinds expressed as arrays-of-tables today,
    but tolerating them keeps the scanner robust.
    """
    result: dict[tuple[str, ...], int] = {}
    for m in _SECTION_HEADER_RE.finditer(text):
        line_num = text.count("\n", 0, m.start()) + 1
        path = _parse_dotted_key(m.group(1))
        if path and path not in result:
            result[path] = line_num
    return result
