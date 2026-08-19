"""Bounded offline access to the canonical packaged release history."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from pathlib import Path

from agentworks.errors import ValidationError

MAX_CHANGELOG_BYTES = 2 * 1024 * 1024
MAX_RELEASE_SECTION_BYTES = 256 * 1024
RELEASE_TOPIC = "concept-release-notes"

_VERSION_COMPONENT = r"(?:0|[1-9][0-9]{0,19})"
_VERSION = rf"{_VERSION_COMPONENT}\.{_VERSION_COMPONENT}\.{_VERSION_COMPONENT}"
_HEADER_RE = re.compile(
    rf"^## \[(?P<version>{_VERSION})\]\(https://github\.com/WayfarerLabs/agentworks/compare/"
    rf"v(?P<from_version>{_VERSION})\.\.\.v(?P<to_version>{_VERSION})\) "
    r"\((?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\)$"
)
_TOPIC_RE = re.compile(
    rf"^{RELEASE_TOPIC}/v(?P<major>{_VERSION_COMPONENT})-"
    rf"(?P<minor>{_VERSION_COMPONENT})-(?P<patch>{_VERSION_COMPONENT})$"
)
_EXPRESSION_MARKERS = ("{{", "}}", "${", "<%", "%>", "{%", "%}")


class ReleaseNotesError(ValidationError):
    """Packaged release history is unavailable or unsafe to render."""


@dataclass(frozen=True, slots=True)
class ReleaseSection:
    """One exact validated section from the packaged changelog."""

    version: str
    body: str

    @property
    def topic(self) -> str:
        return version_topic(self.version)


@dataclass(frozen=True, slots=True)
class ReleaseHistory:
    """A complete validated changelog inventory in source order."""

    sections: tuple[ReleaseSection, ...]

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(section.version for section in self.sections)

    def section(self, version: str) -> ReleaseSection:
        matches = tuple(section for section in self.sections if section.version == version)
        if len(matches) != 1:
            raise ReleaseNotesError(f"packaged release notes for v{version} are unavailable")
        return matches[0]


def version_topic(version: str) -> str:
    """Map one validated stable version to its core-owned topic name."""
    if re.fullmatch(_VERSION, version) is None:
        raise ReleaseNotesError("release version is not a stable MAJOR.MINOR.PATCH value")
    return f"{RELEASE_TOPIC}/v{version.replace('.', '-')}"


def topic_version(topic: str) -> str | None:
    """Return the exact stable version encoded by a dynamic release topic."""
    match = _TOPIC_RE.fullmatch(topic)
    if match is None:
        return None
    return ".".join((match.group("major"), match.group("minor"), match.group("patch")))


def _reject_unsafe_text(text: str) -> None:
    for character in text:
        codepoint = ord(character)
        if (codepoint < 32 and character not in "\t\n\r") or 127 <= codepoint <= 159:
            raise ReleaseNotesError("packaged release history contains terminal control text")
    if any(marker in text for marker in _EXPRESSION_MARKERS):
        raise ReleaseNotesError("packaged release history contains an unsafe expression marker")
    if "⟦" in text or "⟧" in text:
        raise ReleaseNotesError("packaged release history contains a reserved guide delimiter")


def parse_release_history(data: bytes) -> ReleaseHistory:
    """Validate and split one complete release-please changelog."""
    if len(data) > MAX_CHANGELOG_BYTES:
        raise ReleaseNotesError("packaged release history exceeds the 2 MiB limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ReleaseNotesError("packaged release history is not valid UTF-8") from None
    _reject_unsafe_text(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    headers: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        body = line.rstrip("\n")
        if not body.startswith("## "):
            continue
        match = _HEADER_RE.fullmatch(body)
        if match is None:
            raise ReleaseNotesError("packaged release history contains a malformed release header")
        try:
            date.fromisoformat(match.group("date"))
        except ValueError:
            raise ReleaseNotesError("packaged release history contains a malformed release header") from None
        if match.group("to_version") != match.group("version"):
            raise ReleaseNotesError("packaged release history contains a malformed release header")
        headers.append((index, match.group("version")))
    if not headers:
        raise ReleaseNotesError("packaged release history contains no release sections")
    versions = tuple(version for _index, version in headers)
    if len(versions) != len(set(versions)):
        raise ReleaseNotesError("packaged release history contains a duplicate release section")
    sections: list[ReleaseSection] = []
    for position, (line_index, version) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        section = "".join(lines[line_index + 1 : end]).strip()
        if not section:
            raise ReleaseNotesError(f"packaged release notes for v{version} are empty")
        if len(section.encode("utf-8")) > MAX_RELEASE_SECTION_BYTES:
            raise ReleaseNotesError(f"packaged release notes for v{version} exceed the 256 KiB limit")
        sections.append(ReleaseSection(version, section))
    return ReleaseHistory(tuple(sections))


def _source_tree_changelog() -> Path | None:
    """Return the canonical source file only from an identified repository checkout."""
    cli_root = Path(__file__).resolve().parents[1]
    repository_root = cli_root.parent
    if not (cli_root / "pyproject.toml").is_file() or not (repository_root / ".git").exists():
        return None
    return cli_root / "CHANGELOG.md"


def read_release_history() -> ReleaseHistory:
    """Read the fixed wheel resource, with the same canonical file in a checkout."""
    resource = files("agentworks").joinpath("CHANGELOG.md")
    try:
        with resource.open("rb") as stream:
            data = stream.read(MAX_CHANGELOG_BYTES + 1)
    except FileNotFoundError:
        source_changelog = _source_tree_changelog()
        if source_changelog is None:
            raise ReleaseNotesError("packaged release history is unavailable") from None
        try:
            with source_changelog.open("rb") as stream:
                data = stream.read(MAX_CHANGELOG_BYTES + 1)
        except OSError:
            raise ReleaseNotesError("packaged release history is unavailable") from None
    except OSError:
        raise ReleaseNotesError("packaged release history is unavailable") from None
    return parse_release_history(data)
