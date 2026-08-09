#!/usr/bin/env python3
"""Build the deterministic Agentworks static website artifact."""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import stat
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Final, NamedTuple

SITE_BASE_TOKEN: Final = "{{SITE_BASE}}"
SITE_BASE_PATTERN = re.compile(r"/(?:[A-Za-z0-9][A-Za-z0-9._~-]*/)*\Z", re.ASCII)
TOKEN_PATTERN = re.compile(r"{{[A-Z][A-Z0-9_]*}}")
HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+[ \t]*)?$")
FENCE_PATTERN = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(?:[^`~]*)$")
REFERENCE_PATTERN = re.compile(r"^[ ]{0,3}\[([^]]+)\]:[ \t]*(\S*)[ \t]*$")
HTTP_URL_PATTERN = re.compile(r"(?i)https?://")
QUOTED_PROTOCOL_RELATIVE_URL_PATTERN = re.compile(r"""["'`]//""")

INTERIM_NOTICE: Final = (
    "Guided onboarding is not yet published. You can still explore the repository, PyPI package, "
    "rationale, and security model."
)

REPOSITORY_URL: Final = "https://github.com/WayfarerLabs/agentworks"
RATIONALE_URL: Final = f"{REPOSITORY_URL}/blob/main/docs/why-agentworks.md"
CLI_SECRETS_URL: Final = f"{REPOSITORY_URL}/blob/main/cli/README.md#environment-variables-and-secrets"
REPORTING_URL: Final = (
    "https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-"
    "information-about-vulnerabilities/privately-reporting-a-security-vulnerability"
)
APPROVED_EXTERNAL_URLS: Final = frozenset(
    {
        "https://agentworks.build/",
        "https://agentworks.build/security/",
        "https://agentworks.build/404.html",
        REPOSITORY_URL,
        "https://pypi.org/project/agentworks-cli/",
        RATIONALE_URL,
        f"{REPOSITORY_URL}/security/policy",
        f"{REPOSITORY_URL}/issues/224",
        CLI_SECRETS_URL,
        REPORTING_URL,
    }
)

FULL_MANIFEST: Final = frozenset(
    {
        Path("404.html"),
        Path("index.html"),
        Path("assets/agw-rocket.svg"),
        Path("security/index.html"),
        Path("static/lander-game.js"),
        Path("static/lander-model.js"),
        Path("static/lander.css"),
        Path("static/site.css"),
    }
)
FOCUSED_MANIFEST: Final = frozenset(
    {
        Path("404.html"),
        Path("assets/agw-rocket.svg"),
        Path("static/lander-game.js"),
        Path("static/lander-model.js"),
        Path("static/lander.css"),
        Path("static/site.css"),
    }
)
# Compatibility names retained for the accepted focused 404 tests.
EXPECTED_FILES = FOCUSED_MANIFEST
REQUIRED_TEMPLATE_REFERENCES = {
    f'href="{SITE_BASE_TOKEN}"',
    f'href="{SITE_BASE_TOKEN}static/lander.css"',
    f'src="{SITE_BASE_TOKEN}static/lander-game.js"',
    f'href="{SITE_BASE_TOKEN}assets/agw-rocket.svg#agw-mark"',
    f'href="{SITE_BASE_TOKEN}assets/agw-rocket.svg#agw-engine-left"',
    f'href="{SITE_BASE_TOKEN}assets/agw-rocket.svg#agw-engine-right"',
}


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
        ((1, "Why Agentworks"), (2, "Key Principles"), (3, "Declarative Configuration and Templates")),
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

TEMPLATE_TOKENS: Final = {
    "index.html": {
        SITE_BASE_TOKEN,
        "{{HOME_META_DESCRIPTION}}",
        "{{HOME_IDENTITY}}",
    },
    "security.html": {
        SITE_BASE_TOKEN,
        "{{SECURITY_META_DESCRIPTION}}",
        "{{SECURITY_THREATS}}",
        "{{SECURITY_BOUNDARIES}}",
        "{{SECURITY_POSTURE}}",
        "{{SECURITY_SECRETS}}",
        "{{SECURITY_REPORTING}}",
    },
    "404.html": {SITE_BASE_TOKEN},
}
TEMPLATE_REQUIRED_LITERALS: Final = {
    "index.html": {
        "Guided onboarding is not yet published. You can still explore the repository, PyPI package,",
        "View the GitHub repository",
        "View the PyPI package",
        "Read why Agentworks is built this way",
        "We take security seriously.",
        f'href="{REPOSITORY_URL}"',
        'href="https://pypi.org/project/agentworks-cli/"',
        f'href="{RATIONALE_URL}"',
    },
    "security.html": {
        f'href="{REPOSITORY_URL}"',
        'href="https://pypi.org/project/agentworks-cli/"',
        f'href="{REPORTING_URL}"',
        f'href="{REPOSITORY_URL}/security/policy"',
    },
    "404.html": REQUIRED_TEMPLATE_REFERENCES,
}
CONTENT_TOKEN_PLACEMENTS: Final = {
    "index.html": {
        "{{HOME_META_DESCRIPTION}}": ("meta", "description"),
        "{{HOME_IDENTITY}}": ("section-class", "identity-panel"),
    },
    "security.html": {
        "{{SECURITY_META_DESCRIPTION}}": ("meta", "description"),
        "{{SECURITY_THREATS}}": ("section-id", "threat-model"),
        "{{SECURITY_BOUNDARIES}}": ("section-id", "boundaries"),
        "{{SECURITY_POSTURE}}": ("section-id", "operator-posture"),
        "{{SECURITY_SECRETS}}": ("section-id", "credentials"),
        "{{SECURITY_REPORTING}}": ("section-id", "reporting"),
    },
    "404.html": {},
}


class ContractError(ValueError):
    """A fail-closed repository content contract violation."""

    def __init__(self, contract: ContentContract, reason: str) -> None:
        super().__init__(f"{contract.contract_id} {contract.source} {contract.keypath_text}: {reason}")


def validate_site_base(value: str) -> str:
    """Validate an ASCII same-origin path made from safe URL segment characters."""
    if SITE_BASE_PATTERN.fullmatch(value) is None:
        raise ValueError("site base must be an ASCII URL path with safe slash-bounded segments")
    return value


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
            elif destination == "../cli/README.md#environment-variables-and-secrets":
                destination = CLI_SECRETS_URL
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
        _plain_inline(str(home_identity_blocks[0].value), home_identity, references), quote=True
    )
    rendered["SECURITY_META_DESCRIPTION"] = html.escape(
        _plain_inline(str(security_threat_blocks[0].value), security_threats, references), quote=True
    )
    return rendered


def _attribute_map(tag: str, attrs: list[tuple[str, str | None]]) -> dict[str, str | None]:
    names = [name for name, _ in attrs]
    if len(names) != len(set(names)):
        raise ValueError(f"{tag}: duplicate HTML attribute name")
    return dict(attrs)


class _TemplatePlacementParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.placements: dict[str, list[tuple[str, str, tuple[tuple[str, dict[str, str | None]], ...]]]] = {}
        self.exact_text_placements: set[str] = set()
        self.description_content_tokens: set[str] = set()
        self.document_text: list[str] = []
        self.onboarding_text: list[str] = []
        self.onboarding_sections: list[dict[str, str | None]] = []
        self.onboarding_headings = 0
        self.anchor_hrefs: list[str] = []

    def _record_attributes(self, tag: str, attributes: dict[str, str | None]) -> None:
        for attribute, value in attributes.items():
            if value is None:
                continue
            for token in TOKEN_PATTERN.findall(value):
                self.placements.setdefault(token, []).append(("attribute", f"{tag}:{attribute}", tuple(self.stack)))
                if (
                    tag == "meta"
                    and attribute == "content"
                    and attributes.get("name") == "description"
                    and value == token
                ):
                    self.description_content_tokens.add(token)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = _attribute_map(tag, attrs)
        self._record_attributes(tag, attributes)
        if tag == "a" and attributes.get("href") is not None:
            self.anchor_hrefs.append(str(attributes["href"]))
        self.stack.append((tag, attributes))
        if tag == "section" and attributes.get("id") == "onboarding":
            self.onboarding_sections.append(attributes)
        if (
            tag == "h2"
            and attributes.get("id") == "onboarding-heading"
            and any(ancestor == "section" and values.get("id") == "onboarding" for ancestor, values in self.stack[:-1])
        ):
            self.onboarding_headings += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record_attributes(tag, _attribute_map(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.document_text.append(data)
        in_onboarding = any(tag == "section" and attrs.get("id") == "onboarding" for tag, attrs in self.stack)
        if in_onboarding:
            self.onboarding_text.append(data)
        for token in TOKEN_PATTERN.findall(data):
            location = self.stack[-1][0] if self.stack else "document"
            self.placements.setdefault(token, []).append(("text", location, tuple(self.stack)))
            if data.strip() == token:
                self.exact_text_placements.add(token)


def _validate_content_token_placements(name: str, template: str) -> _TemplatePlacementParser:
    parser = _TemplatePlacementParser()
    parser.feed(template)
    for token, (placement_kind, placement_value) in CONTENT_TOKEN_PLACEMENTS[name].items():
        placements = parser.placements.get(token, [])
        if len(placements) != 1:
            raise ValueError(f"{name}: content token {token} must have exactly one parsed placement")
        kind, location, ancestors = placements[0]
        if placement_kind == "meta":
            if (
                kind != "attribute"
                or location != "meta:content"
                or placement_value != "description"
                or token not in parser.description_content_tokens
            ):
                raise ValueError(f"{name}: metadata token {token} must be the exact description content attribute")
            continue
        if kind != "text" or location != "div" or token not in parser.exact_text_placements:
            raise ValueError(f"{name}: block token {token} must be text in its sourced-content container")
        container = ancestors[-1][1]
        if container.get("class") != "sourced-content":
            raise ValueError(f"{name}: block token {token} must be in an exact sourced-content container")
        section = next((attrs for tag, attrs in reversed(ancestors[:-1]) if tag == "section"), None)
        if (
            section is None
            or (placement_kind == "section-id" and section.get("id") != placement_value)
            or (placement_kind == "section-class" and section.get("class") != placement_value)
        ):
            raise ValueError(f"{name}: block token {token} is in the wrong reviewed section")
    return parser


def _validate_interim_template(name: str, parser: _TemplatePlacementParser) -> None:
    if name != "index.html":
        return
    document_text = " ".join("".join(parser.document_text).split())
    onboarding_text = " ".join("".join(parser.onboarding_text).split())
    if document_text.count(INTERIM_NOTICE) != 1 or onboarding_text.count(INTERIM_NOTICE) != 1:
        raise ValueError("index.html: interim availability notice must occur exactly once inside onboarding")
    if len(parser.onboarding_sections) != 1:
        raise ValueError("index.html: exactly one onboarding section is required")
    if parser.onboarding_sections[0].get("aria-labelledby") != "onboarding-heading":
        raise ValueError("index.html: onboarding section must reference onboarding-heading")
    if parser.onboarding_headings != 1 or not onboarding_text:
        raise ValueError("index.html: onboarding must contain its nonempty reviewed heading and notice")
    landing_destinations = (
        REPOSITORY_URL,
        "https://pypi.org/project/agentworks-cli/",
        RATIONALE_URL,
        f"{SITE_BASE_TOKEN}security/",
    )
    if any(parser.anchor_hrefs.count(destination) != 1 for destination in landing_destinations):
        raise ValueError("index.html: each repository, package, rationale, and security destination is required once")
    if len(parser.anchor_hrefs) != 5 or parser.anchor_hrefs.count("#main-content") != 1:
        raise ValueError("index.html: landing anchors must be one skip link plus the four reviewed destinations")


def _validate_template(name: str, template: str) -> None:
    allowed = TEMPLATE_TOKENS[name]
    tokens = TOKEN_PATTERN.findall(template)
    if set(tokens) != allowed:
        raise ValueError(f"{name}: template token vocabulary must be exactly {sorted(allowed)}")
    for token in allowed - {SITE_BASE_TOKEN}:
        if tokens.count(token) != 1:
            raise ValueError(f"{name}: content token {token} must occur exactly once")
    if tokens.count(SITE_BASE_TOKEN) < 1:
        raise ValueError(f"{name}: SITE_BASE must occur at least once")
    masked = TOKEN_PATTERN.sub("", template)
    if "{{" in masked or "}}" in masked:
        raise ValueError(f"{name}: template contains brace-like unknown text")
    for match in re.finditer(re.escape(SITE_BASE_TOKEN), template):
        prefix = template[max(0, match.start() - 160) : match.start()]
        if re.search(r"(?:href|src)=\"[^\"]*$", prefix) is None:
            raise ValueError(f"{name}: SITE_BASE may occur only in URL attributes")
    missing_literals = sorted(literal for literal in TEMPLATE_REQUIRED_LITERALS[name] if literal not in template)
    if missing_literals:
        raise ValueError(f"{name}: template is missing required reviewed literals: {missing_literals}")
    parser = _validate_content_token_placements(name, template)
    _validate_interim_template(name, parser)


def render_named_template(name: str, template: str, site_base: str, substitutions: dict[str, str]) -> str:
    """Render one template through its closed, builder-owned vocabulary."""
    _validate_template(name, template)
    values = {SITE_BASE_TOKEN: site_base}
    values.update({f"{{{{{token}}}}}": value for token, value in substitutions.items()})
    required = TEMPLATE_TOKENS[name]
    missing = sorted(required - values.keys())
    if missing:
        raise ValueError(f"{name}: missing substitutions for required tokens: {missing}")
    for token in required:
        value = values[token]
        if TOKEN_PATTERN.search(value) or "{{" in value or "}}" in value:
            raise ValueError(f"{name}: substitution for {token} contains brace-like token syntax")
    rendered = TOKEN_PATTERN.sub(lambda match: values[match.group(0)], template)
    if TOKEN_PATTERN.search(rendered) or "{{" in rendered or "}}" in rendered:
        raise ValueError(f"{name}: rendered template contains an unexpanded token")
    return rendered


def render_template(template: str, site_base: str) -> str:
    """Render the accepted focused 404 template seam."""
    for reference in REQUIRED_TEMPLATE_REFERENCES:
        if reference not in template:
            raise ValueError(f"template is missing required site-base reference: {reference}")
    return render_named_template("404.html", template, validate_site_base(site_base), {})


class _ReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = _attribute_map(tag, attrs)
        if attributes.get("id"):
            self.ids.add(str(attributes["id"]))
        for name in ("href", "src"):
            if attributes.get(name):
                self.references.append(str(attributes[name]))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _validate_local_references(rendered: dict[Path, bytes], manifest: frozenset[Path], base: str) -> None:
    for path, content in rendered.items():
        if path.suffix != ".html":
            continue
        parser = _ReferenceParser()
        parser.feed(content.decode("utf-8"))
        for reference in parser.references:
            if reference.startswith("#"):
                fragment = reference[1:]
                if not fragment or fragment not in parser.ids:
                    raise ValueError(f"{path}: same-document fragment is absent: {reference}")
                continue
            if reference.startswith("https://"):
                if reference not in APPROVED_EXTERNAL_URLS:
                    raise ValueError(f"{path}: unapproved external URL: {reference}")
                continue
            if not reference.startswith(base):
                raise ValueError(f"{path}: local reference is outside site base: {reference}")
            target = reference[len(base) :].split("#", 1)[0]
            if not target:
                if Path("index.html") not in manifest:
                    continue
                target = "index.html"
            elif target.endswith("/"):
                target += "index.html"
            if Path(target) not in manifest:
                raise ValueError(f"{path}: local reference is absent from manifest: {reference}")


def _validate_runtime_asset(path: Path, source: str) -> None:
    if path.suffix == ".css":
        if re.search(r"(?i)@import\b", source):
            raise ValueError(f"{path}: CSS imports are forbidden")
        if re.search(r"(?i)url\s*\(", source):
            raise ValueError(f"{path}: CSS url() references are forbidden")
        if HTTP_URL_PATTERN.search(source) or QUOTED_PROTOCOL_RELATIVE_URL_PATTERN.search(source):
            raise ValueError(f"{path}: remote CSS URLs are forbidden")
    elif path.suffix == ".js":
        if HTTP_URL_PATTERN.search(source) or QUOTED_PROTOCOL_RELATIVE_URL_PATTERN.search(source):
            raise ValueError(f"{path}: remote JavaScript URLs are forbidden")


def _render_artifact(repo_root: Path, site_base: str, focused: bool) -> tuple[dict[Path, bytes], frozenset[Path]]:
    website = repo_root / "website"
    manifest = FOCUSED_MANIFEST if focused else FULL_MANIFEST
    substitutions = {} if focused else extract_content(repo_root)
    template_names = ("404.html",) if focused else ("404.html", "index.html", "security.html")
    rendered: dict[Path, bytes] = {}
    for name in template_names:
        template = _read_utf8(website / "templates" / name)
        destination = Path("security/index.html") if name == "security.html" else Path(name)
        rendered[destination] = render_named_template(name, template, site_base, substitutions).encode()
    copies = {
        Path("assets/agw-rocket.svg"): website / "assets/agw-rocket.svg",
        Path("static/lander-game.js"): website / "static/lander-game.js",
        Path("static/lander-model.js"): website / "static/lander-model.js",
        Path("static/lander.css"): website / "static/lander.css",
        Path("static/site.css"): website / "static/site.css",
    }
    for destination, source in copies.items():
        content = _read_utf8(source)
        _validate_runtime_asset(destination, content)
        rendered[destination] = content.encode()
    if set(rendered) != manifest:
        raise RuntimeError("rendering invariant failure: artifact does not match selected manifest")
    _validate_local_references(rendered, manifest, site_base)
    return rendered, manifest


def validate_output_location(repo_root: Path, output: Path) -> Path:
    """Return a safe destination without dereferencing its requested final component."""
    if ".." in output.parts or output.name in {"", ".", ".."}:
        raise ValueError("output must name a directory without dot traversal")
    destination = output.parent.resolve() / output.name
    if destination.is_relative_to(repo_root.resolve()):
        raise ValueError("output cannot be the repository or any of its descendants")
    return destination


def _manifest_directories(manifest: frozenset[Path]) -> set[Path]:
    return {parent for path in manifest for parent in path.parents if parent != Path(".")}


def _scan_tree(root: Path) -> tuple[set[Path], set[Path]]:
    files: set[Path] = set()
    directories: set[Path] = set()
    for current, names, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*names, *filenames):
            path = current_path / name
            relative = path.relative_to(root)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"output contains a symlink or special entry: {relative}")
            (directories if stat.S_ISDIR(mode) else files).add(relative)
    return files, directories


def _validate_existing_output(output: Path, manifest: frozenset[Path]) -> None:
    if not output.exists() and not output.is_symlink():
        return
    if output.is_symlink() or not output.is_dir():
        raise ValueError("existing output must be a real directory")
    files, directories = _scan_tree(output)
    if not files.issubset(manifest) or not directories.issubset(_manifest_directories(manifest)):
        raise ValueError("existing output contains entries not owned by the selected builder manifest")


def _verify_manifest(root: Path, manifest: frozenset[Path]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("manifest verification failed: output is not a real directory")
    files, directories = _scan_tree(root)
    if files != manifest or directories != _manifest_directories(manifest):
        raise RuntimeError("manifest verification failed: exact output tree differs")


def _remove_owned_tree(path: Path) -> None:
    if path.exists() and path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _install_staging(staging: Path, output: Path, manifest: frozenset[Path]) -> Path | None:
    backup: Path | None = None
    had_output = output.exists()
    if had_output:
        backup = Path(tempfile.mkdtemp(prefix=f".{output.name}.backup-", dir=output.parent))
        backup.rmdir()
        output.replace(backup)
    try:
        staging.replace(output)
        _verify_manifest(output, manifest)
    except BaseException:
        if output.exists() or output.is_symlink():
            _remove_owned_tree(output)
        if backup is not None and backup.exists():
            backup.replace(output)
        raise
    return backup


def build_site(repo_root: Path, output: Path, site_base: str, *, focused: bool = False) -> None:
    """Build and atomically install the full site or focused 404 artifact."""
    root = repo_root.resolve()
    base = validate_site_base(site_base)
    destination = validate_output_location(root, output)
    rendered, manifest = _render_artifact(root, base, focused)
    _validate_existing_output(destination, manifest)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent))
    backup: Path | None = None
    try:
        for relative in sorted(rendered, key=lambda path: path.as_posix()):
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(rendered[relative])
        _verify_manifest(staging, manifest)
        backup = _install_staging(staging, destination, manifest)
        if backup is not None:
            try:
                shutil.rmtree(backup)
            except OSError:
                print(f"warning: installed output is valid; retained backup at {backup}", file=sys.stderr)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def build_404(repo_root: Path, output: Path, site_base: str) -> None:
    """Build the accepted focused 404 artifact seam."""
    build_site(repo_root, output, site_base, focused=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("404",))
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--site-base", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        build_site(args.repo_root, args.output, args.site_base, focused=args.only == "404")
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
