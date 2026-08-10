"""Repository content projection and closed Markdown rendering."""

from __future__ import annotations

import html
import re
import stat
from html.parser import HTMLParser
from pathlib import Path
from typing import Final, NamedTuple

HEADING_PATTERN = re.compile(r"^[ ]{0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?$")
FENCE_PATTERN = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(?:[^`~]*)$")
REFERENCE_PATTERN = re.compile(r"^[ ]{0,3}\[([^]]+)]\:[ \t]*(\S*)[ \t]*$")
REFERENCE_LINK_PATTERN = re.compile(r"\[([^\[\]\n]+)]\[([^\[\]\n]+)]")
LIST_ITEM_PATTERN = re.compile(r"^( {0,3})[*+-][ \t]+(.+)$")
EMPTY_LIST_ITEM_PATTERN = re.compile(r"^[ ]{0,3}[*+-][ \t]+$")
SETEXT_UNDERLINE_PATTERN = re.compile(r"^[ ]{0,3}(?:=+|-+)[ \t]*$")
UNSUPPORTED_BLOCK_PATTERN = re.compile(
    r"^(?:[ ]{4,}|[ ]{0,3}>[ \t]?|[ ]{0,3}\d+[.)][ \t]+|[ ]{0,3}(?:[-*_][ \t]*){3,}$)"
)
EMAIL_ADDRESS_PATTERN = re.compile(
    r"(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?)+",
    re.IGNORECASE,
)

INTERIM_NOTICE: Final = (
    "Guided onboarding is not yet published. You can still explore the repository, PyPI package, "
    "rationale, and security model."
)

REPOSITORY_URL: Final = "https://github.com/WayfarerLabs/agentworks"
README_SOURCE_URL: Final = f"{REPOSITORY_URL}/blob/main/README.md"
IDEMPOTENCY_URL: Final = f"{REPOSITORY_URL}/blob/main/docs/guides/idempotency.md"
CLI_SECRETS_URL: Final = f"{REPOSITORY_URL}/blob/main/cli/README.md#environment-variables-and-secrets"
PYPI_URL: Final = "https://pypi.org/project/agentworks-cli/"
REPORTING_URL: Final = (
    "https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-"
    "information-about-vulnerabilities/privately-reporting-a-security-vulnerability"
)
SOURCE_RELATIVE_URLS: Final = {
    "../README.md": README_SOURCE_URL,
    "guides/idempotency.md": IDEMPOTENCY_URL,
    "../cli/README.md#environment-variables-and-secrets": CLI_SECRETS_URL,
}
SOURCE_ABSOLUTE_URLS: Final = frozenset(
    {
        f"{REPOSITORY_URL}/issues/224",
        REPORTING_URL,
    }
)


class Block(NamedTuple):
    kind: str
    value: str | tuple[str, ...]


class ContentContract(NamedTuple):
    contract_id: str
    source: Path
    keypath: tuple[tuple[int, str], ...]
    expected: tuple[Block, ...]

    @property
    def keypath_text(self) -> str:
        return " > ".join(f"{'#' * level} {text}" for level, text in self.keypath)


class DocumentContract(NamedTuple):
    contract_id: str
    source: Path
    references: tuple[tuple[str, str], ...] = ()
    github_only_reporting: bool = False


class DocumentBlock(NamedTuple):
    kind: str
    value: str | tuple[str, ...]
    level: int = 0


def paragraph(value: str) -> Block:
    return Block("paragraph", value)


def unordered(*items: str) -> Block:
    return Block("list", items)


CONTRACTS: Final = (
    ContentContract(
        "HOME_IDENTITY",
        Path("README.md"),
        ((1, "Agentworks"),),
        (
            paragraph(
                "A comprehensive toolkit for managing agentic workloads: VMs, workspaces, agents, sessions, "
                "harnesses, secrets/config, and the supporting systems that glue them together. Built around "
                "the conviction that autonomy, security, and control are not mutually exclusive: a good "
                "platform makes it possible and straightforward to have it all."
            ),
            paragraph(
                "Create and manage an agentic fleet from your own workstation. **Durable agents** run as "
                "separate Linux users in **VMs** on infrastructure you choose and control. They retain their "
                "own tools, git credentials, and accumulated application state (a coding assistant's context "
                "and memory, interactive logins). **Disposable sessions** spin up against them for a single "
                "piece of work and are thrown away when done. One `agw` CLI drives all of it declaratively via "
                "an **SSH-over-Tailscale control plane**."
            ),
        ),
    ),
)

MANIFESTO_CONTRACT: Final = DocumentContract("MANIFESTO", Path("docs/why-agentworks.md"))
SECURITY_CONTRACT: Final = DocumentContract(
    "SECURITY",
    Path("SECURITY.md"),
    (("gh-private", REPORTING_URL),),
    True,
)
DOCUMENT_CONTRACTS: Final = (MANIFESTO_CONTRACT, SECURITY_CONTRACT)


class ContractError(ValueError):
    """A fail-closed repository content contract violation."""

    def __init__(self, contract: ContentContract | DocumentContract, reason: str) -> None:
        location = f" {contract.keypath_text}" if isinstance(contract, ContentContract) else ""
        super().__init__(f"{contract.contract_id} {contract.source}{location}: {reason}")


def _read_utf8(
    path: Path,
    contract: ContentContract | DocumentContract | None = None,
) -> str:
    try:
        if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
            raise OSError("input is not a regular file")
        raw = path.read_bytes()
    except OSError as error:
        if contract is not None:
            raise ContractError(contract, "missing/unreadable input") from error
        raise ValueError(f"missing/unreadable input: {path}") from error
    if raw.startswith(b"\xef\xbb\xbf"):
        reason = "invalid UTF-8 or byte-order mark"
        if contract is not None:
            raise ContractError(contract, reason)
        raise ValueError(f"{path}: {reason}")
    try:
        return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as error:
        if contract is not None:
            raise ContractError(contract, "invalid UTF-8 or byte-order mark") from error
        raise ValueError(f"{path}: invalid UTF-8 or byte-order mark") from error


def _fenced_line_indexes(source: str, contract: ContentContract) -> set[int]:
    fenced: set[int] = set()
    fence_character = ""
    fence_length = 0
    lines = source.split("\n")
    for index, line in enumerate(lines):
        if fence_character:
            fenced.add(index)
            closing = re.compile(rf"^[ ]{{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*$")
            if closing.match(line):
                fence_character, fence_length = "", 0
            continue
        fence = FENCE_PATTERN.match(line)
        if fence:
            fenced.add(index)
            marker = fence.group(1)
            fence_character, fence_length = marker[0], len(marker)
    if fence_character:
        raise ContractError(contract, "unsupported block or inline Markdown: unclosed fence")
    return fenced


def _section_ranges(source: str, contract: ContentContract) -> list[tuple[int, int]]:
    headings: list[tuple[int, str, int]] = []
    lines = source.split("\n")
    fenced = _fenced_line_indexes(source, contract)
    for index, line in enumerate(lines):
        if index in fenced:
            continue
        heading = HEADING_PATTERN.match(line)
        if heading:
            headings.append((len(heading.group(1)), heading.group(2).strip(" \t"), index))

    matches: list[tuple[int, int]] = []
    stack: list[tuple[int, str]] = []
    for position, (level, text, line_index) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))
        if tuple(stack) != contract.keypath:
            continue
        end = len(lines)
        for next_level, _, next_index in headings[position + 1 :]:
            if next_level <= level:
                end = next_index
                break
        matches.append((line_index + 1, end))
    if not matches:
        raise ContractError(contract, "missing heading")
    if len(matches) != 1:
        raise ContractError(contract, "duplicate heading")
    return matches


def _normalized_blocks(lines: list[str]) -> list[Block]:
    blocks: list[Block] = []
    index = 0
    while index < len(lines):
        line = lines[index].rstrip(" \t")
        if not line or HEADING_PATTERN.match(line):
            index += 1
            continue
        if line.startswith("- "):
            items: list[str] = []
            while index < len(lines) and lines[index].rstrip(" \t").startswith("- "):
                item = lines[index].rstrip(" \t")[2:]
                index += 1
                continuations: list[str] = []
                while index < len(lines):
                    continuation = lines[index].rstrip(" \t")
                    if re.match(r"^[ ]{2,}\S", continuation):
                        continuations.append(continuation.lstrip(" "))
                        index += 1
                    else:
                        break
                items.append(" ".join((item, *continuations)))
            blocks.append(unordered(*items))
            continue
        paragraph_lines = [line]
        index += 1
        while index < len(lines):
            continuation = lines[index].rstrip(" \t")
            if not continuation or HEADING_PATTERN.match(continuation) or continuation.startswith("- "):
                break
            paragraph_lines.append(continuation)
            index += 1
        blocks.append(paragraph(" ".join(paragraph_lines)))
    return blocks


def _extract(contract: ContentContract, source: str) -> tuple[Block, ...]:
    start, end = _section_ranges(source, contract)[0]
    blocks = _normalized_blocks(source.split("\n")[start:end])
    expected = list(contract.expected)
    matches = [
        index for index in range(len(blocks) - len(expected) + 1) if blocks[index : index + len(expected)] == expected
    ]
    if not matches:
        raise ContractError(contract, "missing expected block sequence or content drift")
    if len(matches) != 1:
        raise ContractError(contract, "duplicate expected block sequence")
    return tuple(blocks[matches[0] : matches[0] + len(expected)])


def _render_inline(
    value: str,
    contract: ContentContract | DocumentContract,
    references: dict[str, str],
) -> str:
    if "![" in value:
        raise ContractError(contract, "unsupported block or inline Markdown")
    result: list[str] = []
    cursor = 0
    patterns = (
        ("strong", re.compile(r"\*\*(?=\S)([^*\n]*?\S)\*\*")),
        (
            "underscore-emphasis",
            re.compile(r"(?<![A-Za-z0-9_])_(?=\S)([^_\n]*?\S)_(?![A-Za-z0-9_])"),
        ),
        ("asterisk-emphasis", re.compile(r"(?<!\*)\*(?=\S)([^*\n]*?\S)\*(?!\*)")),
        ("code", re.compile(r"`([^`\n]+)`")),
        ("inline-link", re.compile(r"\[([^\[\]\n]+)]\(([^()\s]+)\)")),
        ("reference-link", REFERENCE_LINK_PATTERN),
    )
    while cursor < len(value):
        candidates = [
            (match.start(), order, kind, match)
            for order, (kind, pattern) in enumerate(patterns)
            if (match := pattern.search(value, cursor))
        ]
        next_match = min(candidates, default=None)
        end = next_match[0] if next_match else len(value)
        plain = value[cursor:end]
        if re.search(r"[<>\\~*]|!\[|[`\[\]]|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", plain):
            raise ContractError(contract, "unsupported block or inline Markdown")
        result.append(html.escape(plain, quote=True))
        if next_match is None:
            break
        _, _, kind, match = next_match
        inner = match.group(1)
        if kind != "code" and re.search(r"[*_`\[\]<>]", inner):
            raise ContractError(contract, "unsupported block or inline Markdown")
        escaped = html.escape(inner, quote=True)
        if kind == "strong":
            result.append(f"<strong>{escaped}</strong>")
        elif kind in {"underscore-emphasis", "asterisk-emphasis"}:
            result.append(f"<em>{escaped}</em>")
        elif kind == "code":
            result.append(f"<code>{escaped}</code>")
        else:
            destination = match.group(2)
            if kind == "reference-link":
                if destination not in references:
                    raise ContractError(contract, "invalid link")
                destination = references[destination]
            elif destination in SOURCE_RELATIVE_URLS:
                destination = SOURCE_RELATIVE_URLS[destination]
            elif destination not in SOURCE_ABSOLUTE_URLS:
                raise ContractError(contract, "invalid link")
            result.append(f'<a href="{html.escape(destination, quote=True)}">{escaped}</a>')
        cursor = match.end()
    return "".join(result)


def _render_blocks(
    blocks: tuple[Block, ...],
    contract: ContentContract,
    references: dict[str, str],
) -> str:
    rendered: list[str] = []
    for block in blocks:
        if block.kind == "paragraph":
            rendered.append(f"<p>{_render_inline(str(block.value), contract, references)}</p>")
        elif block.kind == "list":
            items = "".join(f"<li>{_render_inline(item, contract, references)}</li>" for item in block.value)
            rendered.append(f"<ul>{items}</ul>")
        else:
            raise ContractError(contract, "unsupported block or inline Markdown")
    return "\n".join(rendered)


def _heading_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _reference_definitions(
    source: str,
    contract: DocumentContract,
) -> tuple[dict[str, str], set[int]]:
    lines = source.split("\n")
    references: dict[str, str] = {}
    definition_lines: set[int] = set()
    for index, line in enumerate(lines):
        fence = FENCE_PATTERN.match(line)
        if fence:
            marker = fence.group(1)
            closing = re.compile(rf"^[ ]{{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$")
            if not any(closing.match(candidate) for candidate in lines[index + 1 :]):
                raise ContractError(contract, "unsupported block or inline Markdown: unclosed fence")
            raise ContractError(contract, "unsupported block or inline Markdown: fenced block")
        match = REFERENCE_PATTERN.match(line)
        if match is None:
            continue
        label, destination = match.groups()
        definition_lines.add(index)
        if not destination:
            if index + 1 >= len(lines) or re.fullmatch(r"[ ]{1,3}\S+", lines[index + 1]) is None:
                raise ContractError(contract, "malformed reference definition")
            destination = lines[index + 1].strip(" \t")
            definition_lines.add(index + 1)
        if label in references:
            raise ContractError(contract, "duplicate reference definition")
        references[label] = destination
    if references != dict(contract.references):
        raise ContractError(contract, "missing or unexpected reference definition")
    return references, definition_lines


def _document_blocks(
    source: str,
    contract: DocumentContract,
) -> tuple[tuple[DocumentBlock, ...], dict[str, str]]:
    references, definition_lines = _reference_definitions(source, contract)
    if contract.github_only_reporting:
        reference_uses = [label for _, label in REFERENCE_LINK_PATTERN.findall(source) if label == "gh-private"]
        if (
            len(reference_uses) != 1
            or EMAIL_ADDRESS_PATTERN.search(source)
        ):
            raise ContractError(contract, "GitHub-only reporting violation")

    lines = source.split("\n")
    blocks: list[DocumentBlock] = []
    index = 0
    previous_heading_level = 0
    heading_ids: set[str] = set()
    h1_count = 0
    while index < len(lines):
        if index in definition_lines or not lines[index].strip(" \t"):
            index += 1
            continue
        raw_line = lines[index]
        if re.search(r" {2,}$", raw_line):
            raise ContractError(contract, "unsupported block or inline Markdown: hard break")
        if EMPTY_LIST_ITEM_PATTERN.match(raw_line):
            raise ContractError(contract, "unsupported block or inline Markdown: empty list item")
        line = raw_line.rstrip(" \t")
        heading = HEADING_PATTERN.match(line)
        if heading:
            level = len(heading.group(1))
            value = heading.group(2).strip(" \t")
            if (
                not value
                or (not blocks and level != 1)
                or (previous_heading_level and level > previous_heading_level + 1)
            ):
                raise ContractError(contract, "malformed heading structure")
            plain_heading = _plain_inline(value, contract, references)
            identifier = _heading_id(plain_heading)
            if not identifier or identifier in heading_ids:
                raise ContractError(contract, "missing or duplicate heading identifier")
            heading_ids.add(identifier)
            h1_count += level == 1
            blocks.append(DocumentBlock("heading", value, level))
            previous_heading_level = level
            index += 1
            continue
        if not blocks or blocks[0].kind != "heading" or blocks[0].level != 1:
            raise ContractError(contract, "document must begin with its single h1")
        if SETEXT_UNDERLINE_PATTERN.match(line) or UNSUPPORTED_BLOCK_PATTERN.match(line):
            raise ContractError(contract, "unsupported block or inline Markdown")
        initial_item = LIST_ITEM_PATTERN.match(line)
        if initial_item:
            list_indent = initial_item.group(1)
            items: list[str] = []
            while index < len(lines) and index not in definition_lines:
                item_line = lines[index]
                if re.search(r" {2,}$", item_line):
                    raise ContractError(contract, "unsupported block or inline Markdown: hard break")
                if EMPTY_LIST_ITEM_PATTERN.match(item_line):
                    raise ContractError(contract, "unsupported block or inline Markdown: empty list item")
                item_match = LIST_ITEM_PATTERN.match(item_line.rstrip(" \t"))
                if item_match is None:
                    break
                if item_match.group(1) != list_indent:
                    raise ContractError(contract, "unsupported block or inline Markdown")
                item = item_match.group(2)
                index += 1
                continuations: list[str] = []
                while index < len(lines) and index not in definition_lines:
                    continuation_line = lines[index]
                    if re.search(r" {2,}$", continuation_line):
                        raise ContractError(contract, "unsupported block or inline Markdown: hard break")
                    if EMPTY_LIST_ITEM_PATTERN.match(continuation_line):
                        raise ContractError(contract, "unsupported block or inline Markdown: empty list item")
                    continuation = continuation_line.rstrip(" \t")
                    next_item = LIST_ITEM_PATTERN.match(continuation)
                    if next_item:
                        if next_item.group(1) != list_indent:
                            raise ContractError(contract, "unsupported block or inline Markdown")
                        break
                    if re.match(r"^[ ]{2,}\S", continuation):
                        stripped = continuation.lstrip(" ")
                        if LIST_ITEM_PATTERN.match(stripped) or SETEXT_UNDERLINE_PATTERN.match(continuation):
                            raise ContractError(contract, "unsupported block or inline Markdown")
                        continuations.append(stripped)
                        index += 1
                    else:
                        break
                items.append(" ".join((item, *continuations)))
            blocks.append(DocumentBlock("list", tuple(items)))
            continue
        paragraph_lines = [line]
        index += 1
        while index < len(lines) and index not in definition_lines:
            continuation_line = lines[index]
            if EMPTY_LIST_ITEM_PATTERN.match(continuation_line):
                raise ContractError(contract, "unsupported block or inline Markdown: empty list item")
            continuation = continuation_line.rstrip(" \t")
            if not continuation or HEADING_PATTERN.match(continuation) or LIST_ITEM_PATTERN.match(continuation):
                break
            if re.search(r" {2,}$", continuation_line):
                raise ContractError(contract, "unsupported block or inline Markdown: hard break")
            if (
                FENCE_PATTERN.match(continuation)
                or SETEXT_UNDERLINE_PATTERN.match(continuation)
                or UNSUPPORTED_BLOCK_PATTERN.match(continuation)
            ):
                raise ContractError(contract, "unsupported block or inline Markdown")
            paragraph_lines.append(continuation)
            index += 1
        blocks.append(DocumentBlock("paragraph", " ".join(paragraph_lines)))

    if h1_count != 1:
        raise ContractError(contract, "document requires exactly one h1")
    return tuple(blocks), references


def _render_document_toc(
    blocks: tuple[DocumentBlock, ...],
    contract: DocumentContract,
    references: dict[str, str],
) -> str:
    sections: list[tuple[DocumentBlock, list[DocumentBlock]]] = []
    for block in blocks:
        if block.kind != "heading" or block.level not in {2, 3}:
            continue
        if block.level == 2:
            sections.append((block, []))
        elif sections:
            sections[-1][1].append(block)
        else:
            raise ContractError(contract, "malformed heading structure")
    if not sections:
        return ""

    def link(block: DocumentBlock) -> str:
        value = str(block.value)
        plain = _plain_inline(value, contract, references)
        label = html.escape(plain)
        identifier = _heading_id(plain)
        return f'<a href="#{identifier}">{label}</a>'

    items: list[str] = []
    for heading, children in sections:
        nested = ""
        if children:
            child_items = "".join(f"<li>{link(child)}</li>" for child in children)
            nested = f'<ol class="page-toc-sublist">{child_items}</ol>'
        items.append(f"<li>{link(heading)}{nested}</li>")
    return (
        '<nav class="page-toc" aria-label="On this page">'
        '<p class="page-toc-title">On this page</p>'
        f'<ol class="page-toc-list">{"".join(items)}</ol></nav>'
    )


def _render_document(source: str, contract: DocumentContract) -> tuple[str, str]:
    blocks, references = _document_blocks(source, contract)
    rendered: list[str] = []
    description = ""
    toc = _render_document_toc(blocks, contract, references)
    for block in blocks:
        if block.kind == "heading":
            plain_heading = _plain_inline(str(block.value), contract, references)
            rendered.append(
                f'<h{block.level} id="{_heading_id(plain_heading)}">'
                f"{_render_inline(str(block.value), contract, references)}</h{block.level}>"
            )
        elif block.kind == "paragraph":
            if not description:
                description = html.escape(_plain_inline(str(block.value), contract, references), quote=True)
            rendered.append(f"<p>{_render_inline(str(block.value), contract, references)}</p>")
        elif block.kind == "list":
            items = "".join(f"<li>{_render_inline(item, contract, references)}</li>" for item in block.value)
            rendered.append(f"<ul>{items}</ul>")
        else:
            raise ContractError(contract, "unsupported block or inline Markdown")
    if not description:
        raise ContractError(contract, "missing meaningful paragraph for metadata")
    if toc:
        output = "\n".join(
            (
                '<div class="long-form-layout">',
                rendered[0],
                toc,
                '<div class="long-form-body">',
                *rendered[1:],
                "</div>",
                "</div>",
            )
        )
    else:
        output = "\n".join(rendered)
    for source_relative in SOURCE_RELATIVE_URLS:
        if f'href="{html.escape(source_relative, quote=True)}"' in output:
            raise ContractError(contract, "unexpanded source-relative link")
    return output, description


def _plain_inline(
    value: str,
    contract: ContentContract | DocumentContract,
    references: dict[str, str],
) -> str:
    rendered = _render_inline(value, contract, references)
    parser = _TextParser()
    parser.feed(rendered)
    return parser.text


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text = ""

    def handle_data(self, data: str) -> None:
        self.text += data


def extract_content(repo_root: Path) -> dict[str, str]:
    """Extract, validate, escape, and render every permanent content contract."""
    home_contract = CONTRACTS[0]
    home_source = _read_utf8(repo_root / home_contract.source, home_contract)
    home_blocks = _extract(home_contract, home_source)
    rendered = {
        home_contract.contract_id: _render_blocks(home_blocks, home_contract, {}),
        "HOME_META_DESCRIPTION": html.escape(
            _plain_inline(str(home_blocks[0].value), home_contract, {}),
            quote=True,
        ),
    }
    for contract in DOCUMENT_CONTRACTS:
        source = _read_utf8(repo_root / contract.source, contract)
        content, description = _render_document(source, contract)
        rendered[f"{contract.contract_id}_CONTENT"] = content
        rendered[f"{contract.contract_id}_META_DESCRIPTION"] = description
    return rendered
