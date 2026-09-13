"""Normalize declared artifact packages into immutable pipeline inputs."""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import replace
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, cast

import yaml

from agentworks.artifacts.model import (
    ArtifactContent,
    ArtifactInput,
    ArtifactMember,
    ArtifactOrigin,
    ArtifactProvenance,
    ArtifactType,
)
from agentworks.package_sources import (
    DEFAULT_CAPTURE_LIMITS,
    CaptureLimits,
    PackageCapture,
    validate_artifact_source,
)
from agentworks.sources import SourceRefError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.artifacts.declarations import ArtifactSpec
    from agentworks.package_sources import CapturedPackage

_TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".txt",
        ".py",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".json",
        ".jsonc",
        ".yaml",
        ".yml",
        ".toml",
    }
)
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def capture_artifacts(
    bundles: Sequence[tuple[str, Mapping[str, ArtifactSpec]]],
    origin: ArtifactOrigin,
    *,
    limits: CaptureLimits = DEFAULT_CAPTURE_LIMITS,
) -> tuple[ArtifactInput, ...]:
    """Capture an owner's declared bundles once, in declaration order.

    Source errors omit source strings and content. The bundle and entry address
    identify the declaration to repair without repeating possibly secret input.
    No previous snapshot can substitute for a failed operation.
    """
    result: list[ArtifactInput] = []
    with PackageCapture(limits) as operation:
        for bundle_name, entries in bundles:
            for entry, spec in entries.items():
                try:
                    operation.check()
                    artifact_type = ArtifactType(spec.type)
                    source = spec.source
                    if source is None:
                        assert spec.type in ("hint", "rule")
                        assert spec.text is not None
                        encoded = spec.text.encode("utf-8")
                        operation.account(len(encoded))
                        content = ArtifactContent(artifact_type, entry, text=normalize_text(encoded))
                        provenance = ArtifactProvenance()
                    else:
                        package = operation.capture(source)
                        content = _content(artifact_type, entry, package, spec.preserve_bytes)
                        provenance = ArtifactProvenance(
                            package.source, package.requested_ref, package.selected_path, package.commit
                        )
                    validate_provenance(provenance)
                    result.append(ArtifactInput(content, provenance, replace(origin, bundle=bundle_name, entry=entry)))
                except SourceRefError as error:
                    raise SourceRefError(
                        f"artifact {bundle_name}/{entry} for {origin.component} {origin.resource_name}: {error}"
                    ) from None
    return tuple(result)


def normalize_text(data: bytes) -> str:
    """Validate source or persisted designated text before computing identity."""
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise SourceRefError("artifact text must be UTF-8") from None
    if "\0" in text:
        raise SourceRefError("artifact text cannot contain NUL")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _content(
    artifact_type: ArtifactType, entry: str, package: CapturedPackage, preserve_bytes: Sequence[str]
) -> ArtifactContent:
    if artifact_type == ArtifactType.SKILL and any(
        fnmatch.fnmatchcase("SKILL.md", pattern) for pattern in preserve_bytes
    ):
        raise SourceRefError("preserve_bytes cannot disable SKILL.md normalization")
    required_text = artifact_type != ArtifactType.SKILL
    members = tuple(
        ArtifactMember(
            member.path,
            normalize_text(member.data).encode("utf-8")
            if required_text or _is_text(member.path, preserve_bytes)
            else member.data,
            member.executable,
            required_text or _is_text(member.path, preserve_bytes),
        )
        for member in package.members
    )
    return content_from_members(artifact_type, entry, members, package.selected_path or package.source)


def content_from_members(
    artifact_type: ArtifactType, entry: str, members: tuple[ArtifactMember, ...], selected_path: str
) -> ArtifactContent:
    """Parse normalized source or persisted members at their respective input boundary."""
    for member in members:
        required_text = artifact_type != ArtifactType.SKILL or member.path == "SKILL.md"
        if required_text and (not member.text or normalize_text(member.data).encode() != member.data):
            raise SourceRefError("artifact entrypoint must contain normalized text")
    if artifact_type in (ArtifactType.HINT, ArtifactType.RULE):
        if len(members) != 1:
            raise SourceRefError("hints and rules require exactly one text file")
        text = normalize_text(members[0].data)
        if not text.strip():
            raise SourceRefError("artifact instructions cannot be empty")
        return ArtifactContent(artifact_type, entry, text=text, members=members)
    if artifact_type == ArtifactType.SKILL:
        entrypoints = [member for member in members if member.path == "SKILL.md"]
        if len(entrypoints) != 1:
            raise SourceRefError("a skill source must select the package containing SKILL.md")
        metadata, body = _frontmatter(normalize_text(entrypoints[0].data))
        name, description = _identity(metadata)
        selected_name = PurePosixPath(selected_path.replace("\\", "/")).name
        if selected_name != name:
            raise SourceRefError("skill name must match its selected package directory")
        _skill_metadata(metadata)
        return ArtifactContent(artifact_type, name, description, body, members, _json(metadata))
    if len(members) != 1:
        raise SourceRefError("an agent source must select one persona Markdown file")
    metadata, body = _frontmatter(normalize_text(members[0].data))
    name, description = _identity(metadata)
    options = metadata.pop("native_options", {})
    if not isinstance(options, dict) or any(not isinstance(value, dict) for value in options.values()):
        raise SourceRefError("agent native_options must map integrations to option objects")
    _reject_execution_options(metadata)
    _reject_execution_options(options)
    return ArtifactContent(artifact_type, name, description, body, members, _json(metadata), _json(options))


def validate_provenance(provenance: ArtifactProvenance) -> None:
    """Validate source provenance crossing the persisted-state boundary.

    Reuse source-reference parsing without resolving paths or contacting a source.
    Local capture records absolute workstation paths; Git capture separates its
    credential-free repository, selection, requested ref and resolved commit.
    """
    source = provenance.source
    if any(
        not value.isprintable()
        for value in (source, provenance.selected_path, provenance.requested_ref, provenance.commit)
        if value
    ):
        raise SourceRefError("artifact provenance must contain printable values")
    if source.startswith(("https://", "git@")):
        reference = "git::" + source
        if provenance.selected_path:
            reference += "//" + provenance.selected_path
        if provenance.requested_ref:
            reference += "?ref=" + provenance.requested_ref
        parsed = validate_artifact_source(reference)
        if (
            parsed.path != source
            or parsed.subpath != provenance.selected_path
            or parsed.ref != provenance.requested_ref
            or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", provenance.commit)
        ):
            raise SourceRefError("artifact Git provenance is inconsistent")
    elif provenance.selected_path or provenance.requested_ref or provenance.commit:
        raise SourceRefError("local and inline artifact provenance cannot contain Git fields")
    elif source != "inline" and not (PurePosixPath(source).is_absolute() or PureWindowsPath(source).is_absolute()):
        raise SourceRefError("local artifact provenance requires an absolute workstation path")


def _is_text(path: str, preserve_bytes: Sequence[str]) -> bool:
    if path == "SKILL.md":
        return True
    return PurePosixPath(path).suffix.lower() in _TEXT_SUFFIXES and not any(
        fnmatch.fnmatchcase(path, pattern) for pattern in preserve_bytes
    )


def _frontmatter(text: str) -> tuple[dict[str, object], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SourceRefError("skill and agent entrypoints require YAML frontmatter")
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise SourceRefError("artifact frontmatter is not terminated")
    header = "".join(lines[1:end])
    if len(header.encode()) > 64 * 1024:
        raise SourceRefError("artifact frontmatter exceeds its size limit")
    try:
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(header)):
            raise SourceRefError("artifact metadata cannot contain YAML aliases or anchors")
        depth = 0
        for event in yaml.parse(header):
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > 32:
                    raise SourceRefError("artifact metadata exceeds its depth limit")
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
        value = yaml.safe_load(header)
    except yaml.YAMLError:
        raise SourceRefError("invalid artifact YAML frontmatter") from None
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SourceRefError("artifact frontmatter must be a metadata object")
    metadata = cast("dict[str, object]", value)
    _json(metadata)
    body = "".join(lines[end + 1 :])
    if not body.strip():
        raise SourceRefError("artifact instructions cannot be empty")
    return metadata, body


def _identity(metadata: dict[str, object]) -> tuple[str, str]:
    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str) or not _NAME.fullmatch(name) or len(name) > 64:
        raise SourceRefError("artifact name must use at most 64 lowercase letters, digits and single hyphens")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise SourceRefError("artifact description must contain between 1 and 1024 characters")
    return name, description


def _skill_metadata(metadata: dict[str, object]) -> None:
    for field in ("license", "compatibility", "allowed-tools"):
        if field in metadata and not isinstance(metadata[field], str):
            raise SourceRefError("standard skill text metadata must be strings")
    compatibility = metadata.get("compatibility", "")
    if isinstance(compatibility, str) and len(compatibility) > 500:
        raise SourceRefError("skill compatibility metadata exceeds 500 characters")
    extra = metadata.get("metadata", {})
    if not isinstance(extra, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in extra.items()
    ):
        raise SourceRefError("standard skill metadata must map strings to strings")
    _reject_execution_options(metadata)


def _reject_execution_options(value: object) -> None:
    if isinstance(value, dict):
        if any(key in value for key in ("hooks", "mcpServers", "mcp_servers")):
            raise SourceRefError("hooks and MCP configuration are not supported artifact inputs")
        for child in value.values():
            _reject_execution_options(child)
    elif isinstance(value, list):
        for child in value:
            _reject_execution_options(child)


def _json(value: object) -> str:
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        if len(result) > 65536:
            raise SourceRefError("artifact metadata exceeds its encoded size limit")
        return result
    except (TypeError, ValueError, RecursionError):
        raise SourceRefError("artifact metadata must contain finite JSON values") from None
