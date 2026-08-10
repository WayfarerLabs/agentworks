"""Repository content projection and closed Markdown rendering."""

from __future__ import annotations

import hashlib
import html
import re
import stat
from html.parser import HTMLParser
from pathlib import Path
from typing import Final, NamedTuple

HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?$")
FENCE_PATTERN = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(?:[^`~]*)$")
REFERENCE_PATTERN = re.compile(r"^[ ]{0,3}\[([^]]+)\]:[ \t]*(\S*)[ \t]*$")

INTERIM_NOTICE: Final = (
    "Guided onboarding is not yet published. You can still explore the repository, PyPI package, "
    "rationale, and security model."
)

REPOSITORY_URL: Final = "https://github.com/WayfarerLabs/agentworks"
README_SOURCE_URL: Final = f"{REPOSITORY_URL}/blob/main/README.md"
IDEMPOTENCY_URL: Final = f"{REPOSITORY_URL}/blob/main/docs/guides/idempotency.md"
CLI_SECRETS_URL: Final = f"{REPOSITORY_URL}/blob/main/cli/README.md#environment-variables-and-secrets"
PYPI_URL: Final = "https://pypi.org/project/agentworks-cli/"
MANIFESTO_SOURCE_SHA256: Final = "dba90181c0c3fca415d965ac4eb3933525044ffe560ec1ef2561be83e875d207"
MANIFESTO_HEADINGS: Final = (
    (1, "Why Agentworks"),
    (2, "The Problem Space"),
    (3, "Security"),
    (3, "Workload Management"),
    (3, "Consistency"),
    (3, "Control"),
    (2, "Key Principles"),
    (3, "Opinionated Consistency"),
    (3, "Composable Isolation"),
    (3, "Ephemerality"),
    (3, "Declarative Configuration and Templates"),
)
SOURCE_RELATIVE_URLS: Final = {
    "../README.md": README_SOURCE_URL,
    "guides/idempotency.md": IDEMPOTENCY_URL,
    "../cli/README.md#environment-variables-and-secrets": CLI_SECRETS_URL,
}
REPORTING_URL: Final = (
    "https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-"
    "information-about-vulnerabilities/privately-reporting-a-security-vulnerability"
)


class Block(NamedTuple):
    kind: str
    value: str | tuple[str, ...]

    @property
    def markdown(self) -> str:
        if self.kind == "paragraph":
            return str(self.value)
        return "\n".join(f"- {item}" for item in self.value)


class ContentContract(NamedTuple):
    contract_id: str
    source: Path
    keypath: tuple[tuple[int, str], ...]
    expected: tuple[Block, ...]

    @property
    def keypath_text(self) -> str:
        return " > ".join(f"{'#' * level} {text}" for level, text in self.keypath)


MANIFESTO_CONTRACT: Final = ContentContract(
    "MANIFESTO",
    Path("docs/why-agentworks.md"),
    ((1, "Why Agentworks"),),
    (),
)


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
                "an SSH-over-Tailscale control plane."
            ),
        ),
    ),
    ContentContract(
        "SECURITY_THREATS",
        Path("docs/why-agentworks.md"),
        ((1, "Why Agentworks"), (2, "The Problem Space"), (3, "Security")),
        (
            paragraph("Agentic engineering is inherently risky. These risks come from multiple directions, including:"),
            unordered(
                "**Honest mistakes** - An agent can simply make a mistake that results in data loss, "
                "corruption, or unintended side effects. It's very easy to find stories of Claude wiping out "
                "entire directories or otherwise causing havoc.",
                "**Prompt injection** - Agents that are exposed to the outside world (e.g. by downloading "
                "untrusted web content) can potentially be manipulated into doing things outside of their "
                "operator's intent or control.",
                "**Supply chain attacks** - Agents may download and run compromised software or dependencies "
                "from external sources, which could introduce malicious code into the environment, at build "
                "time, runtime, or both.",
                "**Rogue agents** - The agent itself could behave maliciously due to a compromise of the model, "
                "the provider, or emergent behavior.",
            ),
        ),
    ),
    ContentContract(
        "SECURITY_BOUNDARIES",
        Path("docs/why-agentworks.md"),
        ((1, "Why Agentworks"), (2, "The Problem Space"), (3, "Security")),
        (
            paragraph(
                "All of these suggest similar solutions, though. You need strong guardrails (isolation, "
                "permissions, etc.) to ensure that _when_ things go sideways, the blast radius is contained "
                "and the operator retains control."
            ),
            paragraph(
                "Being precise about what those guardrails do is as important as having them. Agentworks builds "
                "its isolation from VM boundaries plus standard Linux users, groups, and filesystem permissions. "
                "That separates agents' credentials and state from one another and bounds what a mistaken or "
                "compromised agent can reach. Two things it deliberately does not do: it is not a kernel-level "
                "sandbox (agents on one VM share a kernel, so a local privilege escalation is a path between "
                "them), and it does not yet constrain outbound network access, so an agent that reads untrusted "
                "content can still reach the network with whatever it can read (tracked in "
                "[#224](https://github.com/WayfarerLabs/agentworks/issues/224))."
            ),
        ),
    ),
    ContentContract(
        "SECURITY_POSTURE",
        Path("docs/why-agentworks.md"),
        ((1, "Why Agentworks"), (2, "Key Principles"), (3, "Composable Isolation")),
        (
            paragraph(
                "This model provides several isolation mechanisms, which operators can compose to achieve their "
                "desired security posture. While the system is optimized around the full isolation model (VMs, "
                "agents, and workspaces), this is by no means required. Operators are free to use any subset "
                "that makes sense for their security and operational requirements."
            ),
            paragraph(
                "Composition runs the other way too. Because agents are Linux users and workspaces are Linux "
                "groups, granting _partial_ access costs no more than withholding it, which makes graduated "
                "privilege between cooperating agents a practical everyday pattern rather than a special case. "
                "A research agent can be created with workspace access and nothing else, gather material, and "
                "leave artifacts behind for a more privileged agent to act on, so the privileged agent never "
                "crawls untrusted content itself. Models built on container-per-agent isolation can express the "
                "separation, but pay for the sharing in volumes, networking, or an orchestrator; here both "
                "halves are ordinary filesystem permissions."
            ),
            paragraph(
                "A handoff like that narrows exposure rather than eliminating it. Whatever the low-privilege "
                "agent writes is still attacker-influenced input to whoever reads it next, so those artifacts "
                "are best treated as data to be evaluated, not as instructions to be followed."
            ),
        ),
    ),
    ContentContract(
        "SECURITY_SECRETS",
        Path("docs/why-agentworks.md"),
        (
            (1, "Why Agentworks"),
            (2, "Key Principles"),
            (3, "Declarative Configuration and Templates"),
        ),
        (
            paragraph(
                "Environment variables and secrets are first-class in the configuration: env tables can be "
                "declared at vm, workspace, admin, agent, or session scope and merge in a defined precedence "
                "order. Secret references (`{ secret: name }`) resolve through a configurable backend chain "
                "(`env-var` reads from an `AW_SECRET_*` env var; `prompt` asks interactively at run time). Use "
                "`agw env show` to inspect the merged result for any context. See "
                "[cli/README.md](../cli/README.md#environment-variables-and-secrets) for the shape, and `agw "
                "resource describe-kind secret` for the full reference."
            ),
        ),
    ),
    ContentContract(
        "SECURITY_REPORTING",
        Path("SECURITY.md"),
        ((1, "Security Policy"), (2, "Reporting a Vulnerability")),
        (
            paragraph(
                "If you believe you have found a security vulnerability in Agentworks, please report it "
                "privately rather than opening a public issue."
            ),
            paragraph(
                "Use GitHub's [private vulnerability reporting][gh-private] on this repository, or email the "
                "maintainer directly. Please include:"
            ),
            unordered(
                "A description of the issue and the impact you believe it has.",
                "Steps to reproduce (or a proof-of-concept, if applicable).",
                "The version, commit, or branch you observed it on.",
                "Any relevant configuration (sanitized of secrets).",
            ),
        ),
    ),
)


class ContractError(ValueError):
    """A fail-closed repository content contract violation."""

    def __init__(self, contract: ContentContract, reason: str) -> None:
        super().__init__(f"{contract.contract_id} {contract.source} {contract.keypath_text}: {reason}")


def _read_utf8(path: Path, contract: ContentContract | None = None) -> str:
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


def _reference_url(source: str, reporting: ContentContract) -> str:
    definitions: list[tuple[str, str]] = []
    lines = source.split("\n")
    fenced = _fenced_line_indexes(source, reporting)
    for index, line in enumerate(lines):
        if index in fenced:
            continue
        match = REFERENCE_PATTERN.match(line)
        if match is None:
            continue
        destination = match.group(2)
        if not destination and index + 1 < len(lines):
            continuation = lines[index + 1]
            if index + 1 not in fenced and re.match(r"^[ ]{1,3}\S", continuation):
                destination = continuation.strip(" \t")
        definitions.append((match.group(1), destination))
    matching = [url for label, url in definitions if label == "gh-private"]
    if len(matching) != 1:
        raise ContractError(reporting, "missing or duplicate reference definition")
    if matching[0] != REPORTING_URL:
        raise ContractError(reporting, "reporting-link drift")
    selected = "\n\n".join(block.markdown for block in reporting.expected)
    if selected.count("[private vulnerability reporting][gh-private]") != 1:
        raise ContractError(reporting, "reporting-link drift")
    return matching[0]


def _render_inline(value: str, contract: ContentContract, references: dict[str, str]) -> str:
    if "![" in value:
        raise ContractError(contract, "unsupported block or inline Markdown")
    result: list[str] = []
    cursor = 0
    patterns = (
        ("strong", re.compile(r"\*\*([^*\n]+)\*\*")),
        ("emphasis", re.compile(r"_([^_\n]+)_")),
        ("code", re.compile(r"`([^`\n]+)`")),
        ("inline-link", re.compile(r"\[([^\[\]\n]+)]\(([^()\s]+)\)")),
        ("reference-link", re.compile(r"\[([^\[\]\n]+)]\[([^\[\]\n]+)]")),
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
        if re.search(r"[<>]|!\[|\*\*|[`\[\]]|(?<![A-Za-z0-9])_(?![A-Za-z0-9])", plain):
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
        elif kind == "emphasis":
            result.append(f"<em>{escaped}</em>")
        elif kind == "code":
            result.append(f"<code>{escaped}</code>")
        else:
            destination = match.group(2)
            if kind == "reference-link":
                if destination != "gh-private" or destination not in references:
                    raise ContractError(contract, "invalid link")
                destination = references[destination]
            elif destination in SOURCE_RELATIVE_URLS:
                destination = SOURCE_RELATIVE_URLS[destination]
            elif not destination.startswith("https://"):
                raise ContractError(contract, "invalid link")
            result.append(f'<a href="{html.escape(destination, quote=True)}">{escaped}</a>')
        cursor = match.end()
    return "".join(result)


def _render_blocks(blocks: tuple[Block, ...], contract: ContentContract, references: dict[str, str]) -> str:
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


def _render_manifesto(source: str) -> tuple[str, str]:
    lines = source.split("\n")
    fenced = _fenced_line_indexes(source, MANIFESTO_CONTRACT)
    headings: list[tuple[int, str, int]] = []
    for index, line in enumerate(lines):
        if index in fenced:
            continue
        match = HEADING_PATTERN.match(line)
        if match:
            headings.append((len(match.group(1)), match.group(2).strip(" \t"), index))
    if tuple((level, text) for level, text, _ in headings) != MANIFESTO_HEADINGS:
        raise ContractError(MANIFESTO_CONTRACT, "heading structure drift")
    if hashlib.sha256(source.encode()).hexdigest() != MANIFESTO_SOURCE_SHA256:
        raise ContractError(MANIFESTO_CONTRACT, "content drift")

    rendered: list[str] = []
    first_body = headings[0][2] + 1
    for position, (level, heading, line_index) in enumerate(headings):
        next_index = headings[position + 1][2] if position + 1 < len(headings) else len(lines)
        if level > 1:
            rendered.append(f'<h{level} id="{_heading_id(heading)}">{html.escape(heading)}</h{level}>')
        body_start = first_body if position == 0 else line_index + 1
        blocks = tuple(_normalized_blocks(lines[body_start:next_index]))
        rendered_blocks = _render_blocks(blocks, MANIFESTO_CONTRACT, {})
        if rendered_blocks:
            rendered.append(rendered_blocks)

    output = "\n".join(rendered)
    for source_relative in SOURCE_RELATIVE_URLS:
        if f'href="{html.escape(source_relative, quote=True)}"' in output:
            raise ContractError(MANIFESTO_CONTRACT, "unexpanded source-relative link")
    intro_blocks = tuple(_normalized_blocks(lines[first_body : headings[1][2]]))
    if not intro_blocks or intro_blocks[0].kind != "paragraph":
        raise ContractError(MANIFESTO_CONTRACT, "missing introduction")
    description = html.escape(_plain_inline(str(intro_blocks[0].value), MANIFESTO_CONTRACT, {}), quote=True)
    return output, description


def _plain_inline(value: str, contract: ContentContract, references: dict[str, str]) -> str:
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
    sources: dict[Path, str] = {}
    extracted: dict[str, tuple[ContentContract, tuple[Block, ...]]] = {}
    for contract in CONTRACTS:
        if contract.source not in sources:
            sources[contract.source] = _read_utf8(repo_root / contract.source, contract)
        source = sources[contract.source]
        extracted[contract.contract_id] = (contract, _extract(contract, source))
    reporting = next(contract for contract in CONTRACTS if contract.contract_id == "SECURITY_REPORTING")
    reporting_url = _reference_url(sources[reporting.source], reporting)
    references = {"gh-private": reporting_url}
    rendered = {
        contract_id: _render_blocks(blocks, contract, references)
        for contract_id, (contract, blocks) in extracted.items()
    }
    home_identity, home_identity_blocks = extracted["HOME_IDENTITY"]
    security_threats, security_threat_blocks = extracted["SECURITY_THREATS"]
    rendered["HOME_META_DESCRIPTION"] = html.escape(
        _plain_inline(str(home_identity_blocks[0].value), home_identity, references),
        quote=True,
    )
    rendered["SECURITY_META_DESCRIPTION"] = html.escape(
        _plain_inline(str(security_threat_blocks[0].value), security_threats, references),
        quote=True,
    )
    manifesto_source = sources[Path("docs/why-agentworks.md")]
    rendered["MANIFESTO_CONTENT"], rendered["MANIFESTO_META_DESCRIPTION"] = _render_manifesto(manifesto_source)
    return rendered
