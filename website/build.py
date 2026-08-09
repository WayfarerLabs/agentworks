#!/usr/bin/env python3
"""Build the deterministic Agentworks static website artifact."""

from __future__ import annotations

import argparse
import hashlib
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
README_SOURCE_URL: Final = f"{REPOSITORY_URL}/blob/main/README.md"
IDEMPOTENCY_URL: Final = f"{REPOSITORY_URL}/blob/main/docs/guides/idempotency.md"
CLI_SECRETS_URL: Final = f"{REPOSITORY_URL}/blob/main/cli/README.md#environment-variables-and-secrets"
PYPI_URL: Final = "https://pypi.org/project/agentworks-cli/"
SERVICE_ICON_PATHS: Final = {
    REPOSITORY_URL: (
        "M8 .7a7.5 7.5 0 0 0-2.4 14.6v-2c-1.8.4-2.2-.8-2.2-.8-.3-.8-.8-1-1-1.1-.7-.5.1-.5.1-.5.8.1 "
        "1.2.8 1.2.8.7 1.2 1.8.9 2.2.7.1-.5.3-.9.5-1.1-1.5-.2-3-.7-3-3.3 0-.7.2-1.3.7-1.8-.1-.2-.3-.9.1-1.8 0 "
        "0 .6-.2 2.1.7A7 7 0 0 1 8 4.1a7 7 0 0 1 1.9.3c1.5-.9 2.1-.7 2.1-.7.4.9.2 1.6.1 1.8.5.5.7 1.1.7 1.8 0 "
        "2.6-1.6 3.1-3 3.3.3.2.5.6.5 1.2v3.5A7.5 7.5 0 0 0 8 .7Z"
    ),
    PYPI_URL: (
        "M7.8 1.1c-3.4 0-3.2 1.5-3.2 1.5v1.5h3.3v.5H3.3S1 4.3 1 8s2 3.6 2 3.6h1.2V9.9s-.1-2 2-2h3.3s1.9 0 "
        "1.9-1.8V3s.3-1.9-3.6-1.9Zm-1.8 1a.6.6 0 1 1 0 1.2.6.6 0 0 1 0-1.2Zm2.2 12.8c3.4 0 3.2-1.5 "
        "3.2-1.5v-1.5H8.1v-.5h4.6S15 11.7 15 8s-2-3.6-2-3.6h-1.2v1.7s.1 2-2 2H6.5s-1.9 0-1.9 1.8V13s-.3 1.9 "
        "3.6 1.9Zm1.8-1a.6.6 0 1 1 0-1.2.6.6 0 0 1 0 1.2Z"
    ),
}
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
APPROVED_EXTERNAL_URLS: Final = frozenset(
    {
        "https://agentworks.build/",
        "https://agentworks.build/manifesto/",
        "https://agentworks.build/security/",
        "https://agentworks.build/404.html",
        REPOSITORY_URL,
        PYPI_URL,
        README_SOURCE_URL,
        IDEMPOTENCY_URL,
        f"{REPOSITORY_URL}/security/policy",
        f"{REPOSITORY_URL}/issues/224",
        CLI_SECRETS_URL,
        REPORTING_URL,
    }
)
SHELL_DESTINATION_LABELS: Final = {
    REPOSITORY_URL: "GitHub",
    PYPI_URL: "PyPI",
    f"{SITE_BASE_TOKEN}manifesto/": "Agentworks Manifesto",
    f"{SITE_BASE_TOKEN}security/": "We take security seriously",
}
CURRENT_PAGE_LABELS: Final = {
    "index.html": "Home",
    "manifesto.html": "Manifesto",
    "security.html": "Security",
    "404.html": "404",
}
TEMPLATE_METADATA: Final = {
    "index.html": ("Agentworks", "https://agentworks.build/"),
    "manifesto.html": ("Agentworks Manifesto", "https://agentworks.build/manifesto/"),
    "security.html": ("Security | Agentworks", "https://agentworks.build/security/"),
    "404.html": ("Page not found | Agentworks", "https://agentworks.build/404.html"),
}
TEMPLATE_DESTINATIONS: Final = {
    "index.html": Path("index.html"),
    "manifesto.html": Path("manifesto/index.html"),
    "security.html": Path("security/index.html"),
    "404.html": Path("404.html"),
}
MAIN_ATTRIBUTES: Final = {
    "index.html": {"id": "main-content", "class": "home-main"},
    "manifesto.html": {"id": "main-content", "class": "manifesto-main"},
    "security.html": {"id": "main-content"},
    "404.html": {"id": "main-content"},
}

FULL_MANIFEST: Final = frozenset(
    {
        Path("404.html"),
        Path("index.html"),
        Path("manifesto/index.html"),
        Path("assets/agw-rocket.svg"),
        Path("security/index.html"),
        Path("static/lander-game.js"),
        Path("static/lander-model.js"),
        Path("static/lander.css"),
        Path("static/site.css"),
    }
)
REQUIRED_404_REFERENCES = {
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
    "manifesto.html": {
        SITE_BASE_TOKEN,
        "{{MANIFESTO_META_DESCRIPTION}}",
        "{{MANIFESTO_CONTENT}}",
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
    },
    "manifesto.html": {"Agentworks Manifesto"},
    "security.html": {
        f'href="{REPORTING_URL}"',
        f'href="{REPOSITORY_URL}/security/policy"',
    },
    "404.html": REQUIRED_404_REFERENCES,
}
CONTENT_TOKEN_PLACEMENTS: Final = {
    "index.html": {
        "{{HOME_META_DESCRIPTION}}": ("meta", "description"),
        "{{HOME_IDENTITY}}": ("section-class", "identity-panel"),
    },
    "manifesto.html": {
        "{{MANIFESTO_META_DESCRIPTION}}": ("meta", "description"),
        "{{MANIFESTO_CONTENT}}": ("article-class", "manifesto-content sourced-content"),
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
        _plain_inline(str(home_identity_blocks[0].value), home_identity, references), quote=True
    )
    rendered["SECURITY_META_DESCRIPTION"] = html.escape(
        _plain_inline(str(security_threat_blocks[0].value), security_threats, references), quote=True
    )
    manifesto_source = sources[Path("docs/why-agentworks.md")]
    rendered["MANIFESTO_CONTENT"], rendered["MANIFESTO_META_DESCRIPTION"] = _render_manifesto(manifesto_source)
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
        self.anchors: list[tuple[str, dict[str, str | None], list[str]]] = []
        self.active_anchor_indexes: list[int] = []

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
            self.anchors.append((str(attributes["href"]), attributes, []))
            self.active_anchor_indexes.append(len(self.anchors) - 1)
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
        attributes = _attribute_map(tag, attrs)
        self._record_attributes(tag, attributes)
        if tag == "a" and attributes.get("href") is not None:
            self.anchors.append((str(attributes["href"]), attributes, []))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.active_anchor_indexes:
            self.active_anchor_indexes.pop()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.document_text.append(data)
        for anchor_index in self.active_anchor_indexes:
            self.anchors[anchor_index][2].append(data)
        in_onboarding = any(tag == "section" and attrs.get("id") == "onboarding" for tag, attrs in self.stack)
        if in_onboarding:
            self.onboarding_text.append(data)
        for token in TOKEN_PATTERN.findall(data):
            location = self.stack[-1][0] if self.stack else "document"
            self.placements.setdefault(token, []).append(("text", location, tuple(self.stack)))
            if data.strip() == token:
                self.exact_text_placements.add(token)


class _ShellElement(NamedTuple):
    tag: str
    attributes: dict[str, str | None]
    parent: int | None
    text: list[str]


class _LocalReference(NamedTuple):
    target: Path
    fragment: str | None


class _ShellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[int] = []
        self.elements: list[_ShellElement] = []

    def _append(self, tag: str, attrs: list[tuple[str, str | None]]) -> int:
        parent = self.stack[-1] if self.stack else None
        self.elements.append(_ShellElement(tag, _attribute_map(tag, attrs), parent, []))
        return len(self.elements) - 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack.append(self._append(tag, attrs))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._append(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self.stack) - 1, -1, -1):
            if self.elements[self.stack[position]].tag == tag:
                del self.stack[position:]
                return

    def handle_data(self, data: str) -> None:
        for element_index in self.stack:
            self.elements[element_index].text.append(data)


def _normalized_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _children(parser: _ShellParser, parent: int) -> list[int]:
    return [index for index, element in enumerate(parser.elements) if element.parent == parent]


def _descendants(parser: _ShellParser, parent: int, tag: str | None = None) -> list[int]:
    matches: list[int] = []
    pending = _children(parser, parent)
    while pending:
        index = pending.pop(0)
        element = parser.elements[index]
        if tag is None or element.tag == tag:
            matches.append(index)
        pending[0:0] = _children(parser, index)
    return matches


def _ancestors(parser: _ShellParser, index: int) -> list[int]:
    result: list[int] = []
    parent = parser.elements[index].parent
    while parent is not None:
        result.append(parent)
        parent = parser.elements[parent].parent
    return result


def _hidden(parser: _ShellParser, index: int) -> bool:
    for candidate in (index, *_ancestors(parser, index)):
        attributes = parser.elements[candidate].attributes
        if "hidden" in attributes or attributes.get("aria-hidden") == "true":
            return True
    return False


def _one(parser: _ShellParser, indexes: list[int], reason: str) -> int:
    if len(indexes) != 1:
        raise ValueError(reason)
    return indexes[0]


def _resolve_local_reference(
    reference: str | None, base: str, source: Path | None = None
) -> _LocalReference | None:
    if reference is None:
        return None
    if reference.startswith("#"):
        if source is None:
            return None
        return _LocalReference(source, reference[1:])
    if not reference.startswith(base):
        return None
    location, separator, fragment = reference[len(base) :].partition("#")
    if not location or location == "index.html":
        target = Path("index.html")
    elif location.endswith("/"):
        target = Path(f"{location}index.html")
    else:
        target = Path(location)
    return _LocalReference(target, fragment if separator else None)


def _validate_visible_leaf(
    parser: _ShellParser,
    index: int,
    attributes: dict[str, str | None],
    text: str,
    reason: str,
) -> None:
    element = parser.elements[index]
    if (
        element.attributes != attributes
        or _hidden(parser, index)
        or _children(parser, index)
        or _normalized_text(element.text) != text
    ):
        raise ValueError(reason)


def _validate_service_anchor(parser: _ShellParser, index: int, destination: str, label: str) -> None:
    anchor = parser.elements[index]
    if anchor.tag != "a" or anchor.attributes != {"href": destination}:
        raise ValueError(f"service destination {destination} must use visible label {label!r}")
    if _hidden(parser, index):
        raise ValueError(f"service destination {destination} must remain visible")
    children = _children(parser, index)
    if [parser.elements[child].tag for child in children] != ["svg", "span"]:
        raise ValueError(f"service destination {destination} must contain exactly one icon before its label")
    icon = parser.elements[children[0]]
    if icon.attributes != {
        "class": "service-icon",
        "aria-hidden": "true",
        "focusable": "false",
        "viewbox": "0 0 16 16",
    }:
        raise ValueError(f"service destination {destination} requires one hidden decorative icon")
    icon_children = _children(parser, children[0])
    if (
        len(icon_children) != 1
        or parser.elements[icon_children[0]].tag != "path"
        or parser.elements[icon_children[0]].attributes != {"d": SERVICE_ICON_PATHS[destination]}
        or _children(parser, icon_children[0])
        or _normalized_text(parser.elements[icon_children[0]].text)
    ):
        raise ValueError(f"service destination {destination} requires its exact reviewed icon path")
    _validate_visible_leaf(
        parser,
        children[1],
        {},
        label,
        f"service destination {destination} has a misplaced visible label",
    )
    if _normalized_text(anchor.text) != label:
        raise ValueError(f"service destination {destination} must use visible label {label!r}")


def _validate_shared_shell(name: str, template: str) -> None:
    parser = _ShellParser()
    parser.feed(template)
    elements = parser.elements
    expected_title, expected_canonical = TEMPLATE_METADATA[name]

    if any(element.tag == "style" or "style" in element.attributes for element in elements):
        raise ValueError(f"{name}: inline style cannot alter the reviewed shell visibility")

    html_index = _one(parser, [i for i, element in enumerate(elements) if element.tag == "html"], f"{name}: one html root is required")
    head_index = _one(parser, [i for i in _children(parser, html_index) if elements[i].tag == "head"], f"{name}: one head is required")
    body_index = _one(parser, [i for i in _children(parser, html_index) if elements[i].tag == "body"], f"{name}: one body is required")
    if elements[html_index].attributes != {"lang": "en"} or elements[head_index].attributes or elements[body_index].attributes:
        raise ValueError(f"{name}: html, head, and body root attributes are invalid")
    title_index = _one(parser, [i for i in _children(parser, head_index) if elements[i].tag == "title"], f"{name}: one document title is required")
    canonical_index = _one(
        parser,
        [i for i in _children(parser, head_index) if elements[i].tag == "link" and elements[i].attributes.get("rel") == "canonical"],
        f"{name}: one canonical link is required",
    )
    if _normalized_text(elements[title_index].text) != expected_title:
        raise ValueError(f"{name}: document title must be {expected_title!r}")
    if elements[canonical_index].attributes.get("href") != expected_canonical:
        raise ValueError(f"{name}: canonical URL must be {expected_canonical}")

    body_children = _children(parser, body_index)
    if [elements[index].tag for index in body_children] != ["a", "header", "main", "footer"]:
        raise ValueError(f"{name}: skip link, header, main, and footer must occur once in source order")
    skip_index, header_index, main_index, footer_index = body_children
    _validate_visible_leaf(
        parser,
        skip_index,
        {"class": "skip-link", "href": "#main-content"},
        "Skip to main content",
        f"{name}: exactly one visible reviewed skip link is required",
    )
    if elements[header_index].attributes != {"class": "site-header"}:
        raise ValueError(f"{name}: header requires the exact site-header class")
    if elements[main_index].attributes != MAIN_ATTRIBUTES[name]:
        raise ValueError(f"{name}: main landmark requires its exact CSS-critical attributes")
    if elements[footer_index].attributes != {"class": "site-footer"}:
        raise ValueError(f"{name}: footer requires the exact site-footer class")

    header_children = _children(parser, header_index)
    if name == "index.html":
        if [elements[index].tag for index in header_children] != ["nav", "nav"]:
            raise ValueError(f"{name}: header must contain breadcrumb then external navigation")
        breadcrumb_index, external_index = header_children
    else:
        if [elements[index].tag for index in header_children] != ["div", "nav"]:
            raise ValueError(f"{name}: header must contain identity then external navigation")
        identity_index, external_index = header_children
        if elements[identity_index].attributes != {"class": "header-identity"}:
            raise ValueError(f"{name}: header identity requires its CSS-critical class")
        identity_children = _children(parser, identity_index)
        if [elements[index].tag for index in identity_children] != ["img", "nav"]:
            raise ValueError(f"{name}: small rocket must occur immediately before the breadcrumb")
        mark_index, breadcrumb_index = identity_children
        if elements[mark_index].attributes != {
            "class": "header-mark",
            "src": f"{SITE_BASE_TOKEN}assets/agw-rocket.svg",
            "alt": "",
        }:
            raise ValueError(f"{name}: small header rocket contract is invalid")

    if elements[breadcrumb_index].attributes != {"class": "breadcrumbs", "aria-label": "Breadcrumb"}:
        raise ValueError(f"{name}: breadcrumb requires its exact class and accessible label")
    breadcrumb_children = _children(parser, breadcrumb_index)
    if [elements[index].tag for index in breadcrumb_children] != ["a", "span", "span"]:
        raise ValueError(f"{name}: breadcrumb must contain home, separator, and current item in order")
    home_index, separator_index, current_index = breadcrumb_children
    _validate_visible_leaf(
        parser,
        home_index,
        {"href": SITE_BASE_TOKEN},
        "Agentworks",
        f"{name}: Agentworks home crumb contract is invalid",
    )
    if (
        elements[separator_index].attributes
        != {"class": "breadcrumb-separator", "aria-hidden": "true"}
        or _children(parser, separator_index)
        or _normalized_text(elements[separator_index].text) != "/"
    ):
        raise ValueError(f"{name}: breadcrumb separator contract is invalid")
    _validate_visible_leaf(
        parser,
        current_index,
        {"aria-current": "page"},
        CURRENT_PAGE_LABELS[name],
        f"{name}: breadcrumb current-page state is invalid",
    )

    if elements[external_index].attributes != {"class": "service-links", "aria-label": "External"}:
        raise ValueError(f"{name}: external navigation requires its exact class and accessible label")
    external_children = _children(parser, external_index)
    if [elements[index].tag for index in external_children] != ["a", "a"]:
        raise ValueError(f"{name}: external navigation must contain exactly GitHub then PyPI")
    _validate_service_anchor(parser, external_children[0], REPOSITORY_URL, "GitHub")
    _validate_service_anchor(parser, external_children[1], PYPI_URL, "PyPI")
    service_icons = [index for index, element in enumerate(elements) if element.tag == "svg" and element.attributes.get("class") == "service-icon"]
    expected_svg_count = 3 if name == "404.html" else 2
    if (
        len(_descendants(parser, external_index, "svg")) != 2
        or len(_descendants(parser, header_index, "svg")) != 2
        or len(service_icons) != 2
        or len([element for element in elements if element.tag == "svg"]) != expected_svg_count
    ):
        raise ValueError(f"{name}: header must contain only the two reviewed service icons")

    images = [index for index, element in enumerate(elements) if element.tag == "img"]
    if len(images) != 1:
        raise ValueError(f"{name}: document must contain exactly one reviewed rocket image")
    if name == "index.html":
        hero_index = images[0]
        if elements[hero_index].attributes != {
            "class": "hero-mark",
            "src": f"{SITE_BASE_TOKEN}assets/agw-rocket.svg",
            "alt": "AGW rocket mark",
        }:
            raise ValueError(f"{name}: Home must contain only its reviewed main hero rocket")
        hero_heading_index = elements[hero_index].parent
        if (
            hero_heading_index is None
            or elements[hero_heading_index].attributes != {"class": "hero-heading"}
            or [elements[index].tag for index in _children(parser, hero_heading_index)] != ["img", "div"]
            or main_index not in _ancestors(parser, hero_heading_index)
        ):
            raise ValueError(f"{name}: Home hero rocket must lead the reviewed hero heading")
    elif images[0] != mark_index:
        raise ValueError(f"{name}: the small header rocket must be the document's only image")

    if name == "404.html":
        scene_index = _one(
            parser,
            [index for index, element in enumerate(elements) if element.tag == "svg" and element.attributes.get("id") == "lander-scene"],
            f"{name}: one reviewed lander scene SVG is required",
        )
        if elements[scene_index].attributes != {
            "id": "lander-scene",
            "viewbox": "0 0 1000 640",
            "preserveaspectratio": "xMidYMid meet",
            "role": "img",
            "aria-labelledby": "lander-scene-title lander-scene-description",
        }:
            raise ValueError(f"{name}: lander scene SVG attributes are invalid")
        scene_shell_index = elements[scene_index].parent
        scene_section_index = elements[scene_shell_index].parent if scene_shell_index is not None else None
        if (
            scene_shell_index is None
            or elements[scene_shell_index].attributes != {"id": "lander-scene-shell", "tabindex": "-1"}
            or scene_section_index is None
            or elements[scene_section_index].attributes != {
                "id": "lander-game",
                "aria-label": "Lunar deployment scene",
            }
            or main_index not in _ancestors(parser, scene_section_index)
        ):
            raise ValueError(f"{name}: lander scene SVG must remain inside its reviewed main section")

    footer_children = _children(parser, footer_index)
    if [elements[index].tag for index in footer_children] != ["p", "nav"]:
        raise ValueError(f"{name}: footer ownership must precede footer navigation")
    ownership_index, footer_nav_index = footer_children
    _validate_visible_leaf(
        parser,
        ownership_index,
        {},
        "Product of Wayfarer Labs, LLC",
        f"{name}: footer ownership text is invalid",
    )
    if elements[footer_nav_index].attributes != {"aria-label": "Footer"}:
        raise ValueError(f"{name}: footer navigation label is invalid")
    footer_links = _children(parser, footer_nav_index)
    expected_footer = (
        (f"{SITE_BASE_TOKEN}manifesto/", "Agentworks Manifesto"),
        (f"{SITE_BASE_TOKEN}security/", "We take security seriously"),
    )
    if [elements[index].tag for index in footer_links] != ["a", "a"]:
        raise ValueError(f"{name}: footer must contain exactly Manifesto then Security")
    for index, (destination, label) in zip(footer_links, expected_footer, strict=True):
        _validate_visible_leaf(
            parser,
            index,
            {"href": destination},
            label,
            f"{name}: footer destination {destination} is invalid",
        )

    anchors = [element for element in elements if element.tag == "a"]
    invalid_local_hrefs = [
        anchor.attributes.get("href")
        for anchor in anchors
        if not str(anchor.attributes.get("href") or "").startswith(("#", "https://", SITE_BASE_TOKEN))
    ]
    if invalid_local_hrefs:
        raise ValueError(f"{name}: local template links must use SITE_BASE: {invalid_local_hrefs}")
    for destination, label in {SITE_BASE_TOKEN: "Agentworks", **SHELL_DESTINATION_LABELS}.items():
        matching = [anchor for anchor in anchors if anchor.attributes.get("href") == destination]
        if len(matching) != 1 or _normalized_text(matching[0].text) != label:
            raise ValueError(f"{name}: destination {destination} must occur once with label {label!r}")
    local_routes = [
        reference.target
        for anchor in anchors
        if (
            reference := _resolve_local_reference(
                anchor.attributes.get("href"), SITE_BASE_TOKEN, TEMPLATE_DESTINATIONS[name]
            )
        )
        is not None
        and str(anchor.attributes.get("href")).startswith(SITE_BASE_TOKEN)
    ]
    duplicates = sorted({route.as_posix() for route in local_routes if local_routes.count(route) > 1})
    if duplicates:
        raise ValueError(f"{name}: duplicate normalized local route destinations: {duplicates}")


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
        allowed_locations = {"div"} if placement_kind != "article-class" else {"article"}
        if kind != "text" or location not in allowed_locations or token not in parser.exact_text_placements:
            raise ValueError(f"{name}: block token {token} must be text in its sourced-content container")
        container = ancestors[-1][1]
        if placement_kind == "article-class":
            if container.get("class") != placement_value:
                raise ValueError(f"{name}: block token {token} must be in the reviewed manifesto article")
            continue
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
    _validate_shared_shell(name, template)


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
    parsed: dict[Path, _ReferenceParser] = {}
    for path, content in rendered.items():
        if path.suffix not in {".html", ".svg"}:
            continue
        parser = _ReferenceParser()
        parser.feed(content.decode("utf-8"))
        parsed[path] = parser

    for path, parser in parsed.items():
        for reference in parser.references:
            if reference.startswith("https://"):
                if reference not in APPROVED_EXTERNAL_URLS:
                    raise ValueError(f"{path}: unapproved external URL: {reference}")
                continue
            local = _resolve_local_reference(reference, base, path)
            if local is None:
                raise ValueError(f"{path}: local reference is outside site base: {reference}")
            if local.target not in manifest:
                raise ValueError(f"{path}: local reference is absent from manifest: {reference}")
            if local.fragment is not None and (
                not local.fragment
                or local.target not in parsed
                or local.fragment not in parsed[local.target].ids
            ):
                raise ValueError(f"{path}: local reference fragment is absent: {reference}")


def _validate_runtime_asset(path: Path, source: str) -> None:
    if path.suffix == ".css":
        if re.search(r"(?i)@import\b", source):
            raise ValueError(f"{path}: CSS imports are forbidden")
        if re.search(r"(?i)url\s*\(", source):
            raise ValueError(f"{path}: CSS url() references are forbidden")
        if HTTP_URL_PATTERN.search(source) or QUOTED_PROTOCOL_RELATIVE_URL_PATTERN.search(source):
            raise ValueError(f"{path}: remote CSS URLs are forbidden")
        if path == Path("static/site.css"):
            without_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
            declarations = (
                (match.group(1).lower(), re.sub(r"\s+", "", match.group(2)).lower())
                for match in re.finditer(r"(?:^|[;{])\s*([A-Za-z-]+)\s*:\s*([^;{}]+)", without_comments)
            )
            forbidden = {"opacity", "visibility", "content-visibility"}
            display_values = {"grid", "flex", "inline-flex"}
            if any(
                property_name in forbidden
                or (property_name == "display" and value not in display_values)
                for property_name, value in declarations
            ):
                raise ValueError(f"{path}: shared CSS declaration is outside the reviewed layout contract")
    elif path.suffix == ".js":
        if HTTP_URL_PATTERN.search(source) or QUOTED_PROTOCOL_RELATIVE_URL_PATTERN.search(source):
            raise ValueError(f"{path}: remote JavaScript URLs are forbidden")


def _render_artifact(repo_root: Path, site_base: str) -> tuple[dict[Path, bytes], frozenset[Path]]:
    website = repo_root / "website"
    manifest = FULL_MANIFEST
    substitutions = extract_content(repo_root)
    template_names = ("404.html", "index.html", "manifesto.html", "security.html")
    rendered: dict[Path, bytes] = {}
    for name in template_names:
        template = _read_utf8(website / "templates" / name)
        destination = TEMPLATE_DESTINATIONS[name]
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
        raise RuntimeError("rendering invariant failure: artifact does not match complete manifest")
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


def build_site(repo_root: Path, output: Path, site_base: str) -> None:
    """Build and atomically install the complete linked site."""
    root = repo_root.resolve()
    base = validate_site_base(site_base)
    destination = validate_output_location(root, output)
    rendered, manifest = _render_artifact(root, base)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--site-base", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        build_site(args.repo_root, args.output, args.site_base)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
