#!/usr/bin/env python3
"""Generate the canonical Agentworks assistance package projections."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

README_BEGIN = b"<!-- BEGIN GENERATED AGENTWORKS ASSISTANCE -->"
README_END = b"<!-- END GENERATED AGENTWORKS ASSISTANCE -->"
CANONICAL_BODY = Path("packaging/agentworks/assistance.md")
METADATA_FILE = Path("packaging/agentworks/metadata.json")
README_FILE = Path("README.md")

CLAUDE_ROOT = Path("plugins/claude-code/agentworks")
CODEX_ROOT = Path("plugins/codex/agentworks")
CLAUDE_MANIFEST = CLAUDE_ROOT / ".claude-plugin/plugin.json"
CLAUDE_SKILL = CLAUDE_ROOT / "skills/agentworks/SKILL.md"
CODEX_MANIFEST = CODEX_ROOT / ".codex-plugin/plugin.json"
CODEX_SKILL = CODEX_ROOT / "skills/agentworks/SKILL.md"
CLAUDE_MARKETPLACE = Path(".claude-plugin/marketplace.json")
CODEX_MARKETPLACE = Path(".agents/plugins/marketplace.json")

PLUGIN_OUTPUTS = (
    CLAUDE_MARKETPLACE,
    CODEX_MARKETPLACE,
    CLAUDE_MANIFEST,
    CLAUDE_SKILL,
    CODEX_MANIFEST,
    CODEX_SKILL,
)
GENERATED_OUTPUTS = (*PLUGIN_OUTPUTS, README_FILE)
ROOT_INVENTORIES = {
    CLAUDE_ROOT: frozenset({CLAUDE_MANIFEST, CLAUDE_SKILL}),
    CODEX_ROOT: frozenset({CODEX_MANIFEST, CODEX_SKILL}),
}

_SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_PACKAGE_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class GenerationError(ValueError):
    """Raised when canonical package input or generated inventory is invalid."""


@dataclass(frozen=True)
class Publisher:
    name: str
    url: str


@dataclass(frozen=True)
class Interface:
    short_description: str
    long_description: str
    category: str
    capabilities: tuple[str, ...]
    default_prompt: tuple[str, ...]


@dataclass(frozen=True)
class Metadata:
    name: str
    package_version: str
    minimum_cli_version: str
    description: str
    publisher: Publisher
    homepage: str
    repository: str
    license: str
    display_name: str
    skill_description: str
    interface: Interface


def _object(value: Any, *, field: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise GenerationError(f"{field} must be an object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        unknown = sorted(actual - keys)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise GenerationError(f"{field} fields are invalid: {'; '.join(details)}")
    return value


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise GenerationError(f"{field} must be a non-empty trimmed string")
    if "\n" in value or "\r" in value:
        raise GenerationError(f"{field} must be a single line")
    return value


def _https(value: Any, *, field: str) -> str:
    text = _text(value, field=field)
    if not text.startswith("https://"):
        raise GenerationError(f"{field} must be an absolute HTTPS URL")
    return text


def _text_list(value: Any, *, field: str, maximum: int | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise GenerationError(f"{field} must be a non-empty list")
    if maximum is not None and len(value) > maximum:
        raise GenerationError(f"{field} must contain at most {maximum} entries")
    return tuple(_text(item, field=f"{field} entry") for item in value)


def load_metadata(path: Path) -> Metadata:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GenerationError(f"cannot read {path}: {exc}") from exc

    root = _object(
        raw,
        field="metadata",
        keys=frozenset(
            {
                "name",
                "packageVersion",
                "minimumCliVersion",
                "description",
                "publisher",
                "homepage",
                "repository",
                "license",
                "displayName",
                "skillDescription",
                "interface",
            }
        ),
    )
    publisher_raw = _object(root["publisher"], field="metadata.publisher", keys=frozenset({"name", "url"}))
    interface_raw = _object(
        root["interface"],
        field="metadata.interface",
        keys=frozenset({"shortDescription", "longDescription", "category", "capabilities", "defaultPrompt"}),
    )

    name = _text(root["name"], field="metadata.name")
    if not _PACKAGE_NAME.fullmatch(name):
        raise GenerationError("metadata.name must use lower-case hyphen-case")
    package_version = _text(root["packageVersion"], field="metadata.packageVersion")
    minimum_cli_version = _text(root["minimumCliVersion"], field="metadata.minimumCliVersion")
    if not _SEMVER.fullmatch(package_version):
        raise GenerationError("metadata.packageVersion must be strict semantic versioning")
    if not _SEMVER.fullmatch(minimum_cli_version):
        raise GenerationError("metadata.minimumCliVersion must be a stable semantic version")

    return Metadata(
        name=name,
        package_version=package_version,
        minimum_cli_version=minimum_cli_version,
        description=_text(root["description"], field="metadata.description"),
        publisher=Publisher(
            name=_text(publisher_raw["name"], field="metadata.publisher.name"),
            url=_https(publisher_raw["url"], field="metadata.publisher.url"),
        ),
        homepage=_https(root["homepage"], field="metadata.homepage"),
        repository=_https(root["repository"], field="metadata.repository"),
        license=_text(root["license"], field="metadata.license"),
        display_name=_text(root["displayName"], field="metadata.displayName"),
        skill_description=_text(root["skillDescription"], field="metadata.skillDescription"),
        interface=Interface(
            short_description=_text(interface_raw["shortDescription"], field="metadata.interface.shortDescription"),
            long_description=_text(interface_raw["longDescription"], field="metadata.interface.longDescription"),
            category=_text(interface_raw["category"], field="metadata.interface.category"),
            capabilities=_text_list(interface_raw["capabilities"], field="metadata.interface.capabilities"),
            default_prompt=_text_list(
                interface_raw["defaultPrompt"], field="metadata.interface.defaultPrompt", maximum=3
            ),
        ),
    )


def _json_bytes(value: object) -> bytes:
    lines = json.dumps(value, ensure_ascii=False, indent=2).splitlines()
    compacted: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.rstrip().endswith("["):
            closing = index + 1
            entries: list[str] = []
            while closing < len(lines) and not lines[closing].lstrip().startswith("]"):
                entry = lines[closing].strip()
                if not entry.startswith('"'):
                    entries = []
                    break
                entries.append(entry)
                closing += 1
            if entries and closing < len(lines):
                suffix = "," if lines[closing].rstrip().endswith(",") else ""
                candidate = f"{line.rstrip()}{' '.join(entries)}]{suffix}"
                if len(candidate) <= 100:
                    compacted.append(candidate)
                    index = closing + 1
                    continue
        compacted.append(line)
        index += 1
    return ("\n".join(compacted) + "\n").encode()


def _folded_yaml(field: str, value: str) -> str:
    lines = textwrap.wrap(value, width=98, break_long_words=False, break_on_hyphens=False)
    return f"{field}: >-\n" + "".join(f"  {line}\n" for line in lines)


def _skill(metadata: Metadata, body: bytes) -> bytes:
    frontmatter = (
        "---\n"
        f"name: {metadata.name}\n"
        + _folded_yaml("description", metadata.skill_description)
        + _folded_yaml(
            "compatibility",
            "Requires network and operator-approved workstation access when the requested task needs them.",
        )
        + "metadata:\n"
        f"  agentworks-package-version: {json.dumps(metadata.package_version)}\n"
        f"  agentworks-min-cli-version: {json.dumps(metadata.minimum_cli_version)}\n"
        "---\n\n"
    ).encode()
    return frontmatter + body


def _longest_backtick_run(body: bytes) -> int:
    longest = 0
    current = 0
    for byte in body:
        if byte == ord("`"):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _readme_projection(readme: bytes, body: bytes) -> bytes:
    if readme.count(README_BEGIN) != 1 or readme.count(README_END) != 1:
        raise GenerationError("README must contain exactly one assistance marker pair")
    begin = readme.index(README_BEGIN) + len(README_BEGIN)
    end = readme.index(README_END)
    if begin >= end:
        raise GenerationError("README assistance markers are out of order")
    fence = b"`" * max(3, _longest_backtick_run(body) + 1)
    region = b"\n\n" + fence + b"markdown\n" + body + fence + b"\n\n"
    return readme[:begin] + region + readme[end:]


def _manifest_base(metadata: Metadata) -> dict[str, object]:
    return {
        "name": metadata.name,
        "version": metadata.package_version,
        "description": metadata.description,
        "author": {"name": metadata.publisher.name, "url": metadata.publisher.url},
        "homepage": metadata.homepage,
        "repository": metadata.repository,
        "license": metadata.license,
    }


def render_outputs(root: Path) -> dict[Path, bytes]:
    metadata = load_metadata(root / METADATA_FILE)
    try:
        body = (root / CANONICAL_BODY).read_bytes()
        readme = (root / README_FILE).read_bytes()
    except OSError as exc:
        raise GenerationError(f"cannot read canonical input: {exc}") from exc
    if not body or not body.endswith(b"\n"):
        raise GenerationError(f"{CANONICAL_BODY} must be non-empty and end with a newline")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GenerationError(f"{CANONICAL_BODY} must be UTF-8") from exc
    if b"\r" in body:
        raise GenerationError(f"{CANONICAL_BODY} must use LF line endings")

    claude_manifest = _manifest_base(metadata)
    codex_manifest = {
        **_manifest_base(metadata),
        "skills": "./skills/",
        "interface": {
            "displayName": metadata.display_name,
            "shortDescription": metadata.interface.short_description,
            "longDescription": metadata.interface.long_description,
            "developerName": metadata.publisher.name,
            "category": metadata.interface.category,
            "capabilities": list(metadata.interface.capabilities),
            "defaultPrompt": list(metadata.interface.default_prompt),
        },
    }
    claude_marketplace = {
        "$schema": "https://json.schemastore.org/claude-code-marketplace.json",
        "name": metadata.name,
        "description": metadata.description,
        "owner": {"name": metadata.publisher.name},
        "plugins": [
            {
                "name": metadata.name,
                "description": metadata.description,
                "source": f"./{CLAUDE_ROOT.as_posix()}",
            }
        ],
    }
    codex_marketplace = {
        "name": metadata.name,
        "interface": {"displayName": metadata.display_name},
        "plugins": [
            {
                "name": metadata.name,
                "source": {"source": "local", "path": f"./{CODEX_ROOT.as_posix()}"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": metadata.interface.category,
            }
        ],
    }
    skill = _skill(metadata, body)
    return {
        CLAUDE_MARKETPLACE: _json_bytes(claude_marketplace),
        CODEX_MARKETPLACE: _json_bytes(codex_marketplace),
        CLAUDE_MANIFEST: _json_bytes(claude_manifest),
        CLAUDE_SKILL: skill,
        CODEX_MANIFEST: _json_bytes(codex_manifest),
        CODEX_SKILL: skill,
        README_FILE: _readme_projection(readme, body),
    }


def _inventory(root: Path) -> set[Path]:
    unexpected: set[Path] = set()
    for plugin_root, expected in ROOT_INVENTORIES.items():
        absolute = root / plugin_root
        if not absolute.exists():
            continue
        for path in absolute.rglob("*"):
            if path.is_file() or path.is_symlink():
                relative = path.relative_to(root)
                if relative not in expected:
                    unexpected.add(relative)
    return unexpected


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def generate(root: Path, *, check: bool) -> tuple[Path, ...]:
    root = root.resolve()
    unexpected = _inventory(root)
    if unexpected:
        paths = ", ".join(path.as_posix() for path in sorted(unexpected))
        raise GenerationError(f"unexpected files in generated plugin roots: {paths}")

    outputs = render_outputs(root)
    mismatches = tuple(
        path
        for path, expected in outputs.items()
        if not (root / path).is_file() or (root / path).read_bytes() != expected
    )
    if check:
        return mismatches
    for path in mismatches:
        _atomic_write(root / path, outputs[path])
    return mismatches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    try:
        mismatches = generate(root, check=args.check)
    except GenerationError as exc:
        parser.error(str(exc))
    if args.check and mismatches:
        for path in mismatches:
            print(f"generated package is stale: {path.as_posix()}")
        return 1
    if not args.check:
        for path in mismatches:
            print(f"generated: {path.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
