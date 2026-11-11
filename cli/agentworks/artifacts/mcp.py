"""Strict, unresolved MCP connection definitions at declaration and capture boundaries."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Annotated, Literal, cast
from urllib.parse import urlsplit

import json5
from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from agentworks.artifacts.model import ArtifactContent, ArtifactType
from agentworks.schema import AgwModel
from agentworks.sources import SourceRefError

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_DEFINITION_FIELDS = frozenset({"type", "description", "command", "args", "env", "url", "headers"})


def _public_string(value: str) -> str:
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise ValueError("unsupported control character in MCP field")
    return value


def _literal(value: str) -> str:
    _public_string(value)
    if "${" in value or "$(" in value or "`" in value:
        raise ValueError("MCP field requires a literal value")
    return value


class _MCPDefinition(AgwModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    description: str | None = None

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        if value is not None:
            _public_string(value)
            if not value.strip():
                raise ValueError("MCP description must not be blank")
        return value


class MCPStdioDefinition(_MCPDefinition):
    """A target-runtime executable and unresolved child environment."""

    type: Literal["stdio"]
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def _command(cls, value: str) -> str:
        _literal(value)
        if not value.strip():
            raise ValueError("MCP command must not be blank")
        return value

    @field_validator("args")
    @classmethod
    def _arguments(cls, values: list[str]) -> list[str]:
        for value in values:
            _literal(value)
        return values

    @field_validator("env")
    @classmethod
    def _environment(cls, values: dict[str, str]) -> dict[str, str]:
        for name, value in values.items():
            if _ENV_NAME.fullmatch(name) is None:
                raise ValueError("MCP env requires POSIX variable names")
            _public_string(value)
            if value != f"${{{name}}}":
                _literal(value)
        return values


class MCPHTTPDefinition(_MCPDefinition):
    """A Streamable HTTP endpoint with literal or unresolved header values."""

    type: Literal["http"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        _literal(value)
        try:
            parts = urlsplit(value)
            port = parts.port
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or any(character.isspace() for character in value)
                or (port is not None and not 0 < port <= 65535)
            ):
                raise ValueError
        except ValueError:
            raise ValueError("MCP url must be an absolute HTTP(S) endpoint without userinfo") from None
        return value

    @field_validator("headers")
    @classmethod
    def _headers(cls, values: dict[str, str]) -> dict[str, str]:
        seen: set[str] = set()
        for name, value in values.items():
            if _HEADER_NAME.fullmatch(name) is None or name.lower() in seen:
                raise ValueError("MCP headers require unique case-insensitive HTTP field names")
            seen.add(name.lower())
            _public_string(value)
            reference = value[7:] if name.lower() == "authorization" and value.startswith("Bearer ") else value
            if _REFERENCE.fullmatch(reference) is None:
                _literal(value)
        return values


MCPDefinition = Annotated[MCPStdioDefinition | MCPHTTPDefinition, Field(discriminator="type")]
_DEFINITION = TypeAdapter(MCPDefinition)


def parse_definition(value: object) -> MCPStdioDefinition | MCPHTTPDefinition:
    """Validate untrusted source or persisted data without exposing rejected values."""
    try:
        return _DEFINITION.validate_python(value)
    except ValidationError as error:
        raise SourceRefError(definition_error(error)) from None


def definition_error(error: ValidationError) -> str:
    """Pydantic's discriminator messages can otherwise echo rejected field values."""
    rows = error.errors(include_input=False)
    fields = sorted(
        {part for row in rows for part in row["loc"] if isinstance(part, str) and part in _DEFINITION_FIELDS}
    )
    if any(row["type"] == "extra_forbidden" for row in rows):
        fields.append("unexpected fields")
    if not fields:
        fields = ["definition"]
    return f"invalid MCP definition field(s): {', '.join(fields)}"


def content_from_definition(name: str, definition: MCPDefinition) -> ArtifactContent:
    """Build immutable canonical content from an already validated definition."""
    metadata = definition.model_dump(mode="json", exclude_none=True)
    return ArtifactContent(
        ArtifactType.MCP_SERVER,
        name,
        definition.description or "",
        metadata_json=json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    )


def content_from_source(name: str, text: str) -> ArtifactContent:
    """Check the entire JSONC document, then interpret only the same-named server."""
    try:
        value: object = json5.loads(text, allow_duplicate_keys=False)
    except (ValueError, RecursionError):
        raise SourceRefError("MCP source must contain valid JSON or JSONC") from None
    if (
        not isinstance(value, dict)
        or set(value) - {"mcpServers", "$schema"}
        or not isinstance(value.get("mcpServers"), dict)
        or ("$schema" in value and not isinstance(value["$schema"], str))
    ):
        raise SourceRefError("MCP source requires mcpServers and optional string $schema")
    servers = cast("dict[str, object]", value["mcpServers"])
    if name not in servers:
        raise SourceRefError("MCP source has no server matching the artifact key")
    return content_from_definition(name, parse_definition(servers[name]))
