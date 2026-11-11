"""Static MCP translation and entry ownership within shared native documents."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Literal, cast

from agentworks import output
from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral
from agentworks.artifacts.model import ArtifactInput, ArtifactInputs, ArtifactType
from agentworks.artifacts.names import mcp_native_name
from agentworks.capabilities.harness_integration.settings import (
    SettingsFormat,
    SettingsObject,
    SettingsValue,
    parse_settings,
    serialize_settings,
)
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, WorkspaceSetupInvocation
from agentworks.errors import ConfigError, ConnectivityError, ExternalError, StateError
from agentworks.harness_setup.model import NativeClaim
from agentworks.native_files import NativeFiles, native_path

MCP_CLAIM_ROLE = "artifact-mcp-server"
type MCPTool = Literal["claude", "codex", "grok"]
# The mapping returns its policy-winning contributions separately from retained
# native values, so canonical entries can distinguish an explicit collision.
type DocumentMapping = Callable[[bytes | None], tuple[bytes | None, SettingsObject]]
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_mcp(item: ArtifactInput, tool: MCPTool) -> SettingsObject:
    """Translate validated metadata without reading credentials or environment."""
    metadata = item.content.metadata
    if metadata["type"] == "stdio":
        result: SettingsObject = {
            "command": cast("str", metadata["command"]),
            "args": cast("list[SettingsValue]", metadata["args"]),
        }
        environment = cast("dict[str, str]", metadata["env"])
        if tool == "codex":
            literals: SettingsObject = {}
            forwarded: list[SettingsValue] = []
            for name, value in environment.items():
                reference = _REFERENCE.fullmatch(value)
                if reference:
                    forwarded.append(reference[1])
                else:
                    literals[name] = value
            if literals:
                result["env"] = literals
            if forwarded:
                result["env_vars"] = forwarded
        elif environment:
            result["env"] = cast("SettingsObject", environment)
    else:
        result = {"url": cast("str", metadata["url"])}
        headers = cast("dict[str, str]", metadata["headers"])
        if tool == "codex":
            literals = {}
            references: SettingsObject = {}
            for name, value in headers.items():
                reference = _REFERENCE.fullmatch(value)
                bearer = _REFERENCE.fullmatch(value.removeprefix("Bearer ")) if value.startswith("Bearer ") else None
                if reference:
                    references[name] = reference[1]
                elif name.lower() == "authorization" and bearer:
                    result["bearer_token_env_var"] = bearer[1]
                else:
                    literals[name] = value
            if literals:
                result["http_headers"] = literals
            if references:
                result["env_http_headers"] = references
        elif headers:
            result["headers"] = cast("SettingsObject", headers)
    if tool == "claude":
        result["type"] = cast("str", metadata["type"])
    return result


def mcp_servers(inputs: ArtifactInputs, tool: MCPTool) -> SettingsObject:
    """Return native entries for private session configurations and overrides."""
    result: SettingsObject = {}
    for item in inputs.items():
        if item.content.type == ArtifactType.MCP_SERVER:
            name = mcp_native_name(item)
            if name in result:
                raise ConfigError("MCP server native names collide")
            result[name] = render_mcp(item, tool)
    return result


def _fingerprint(value: SettingsValue) -> str:
    content = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(content).hexdigest()


def _table(document: SettingsObject, key: str) -> SettingsObject:
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError("native MCP servers must use an object or table")
    return value


def _shared_paths(
    invocation: UserSetupInvocation | WorkspaceSetupInvocation, claims: tuple[NativeClaim, ...]
) -> tuple[str, ...]:
    return (
        tuple(sorted({claim.destination for claim in claims if claim.role == MCP_CLAIM_ROLE}))
        if isinstance(invocation, WorkspaceSetupInvocation)
        else ()
    )


def apply_mcp(
    tool: MCPTool,
    invocation: UserSetupInvocation | WorkspaceSetupInvocation,
    *,
    destination: str,
    map_document: DocumentMapping | None = None,
    claims: tuple[NativeClaim, ...] | None = None,
) -> ArtifactApplication:
    """Apply entries once, checkpointing only confirmed document mutations.

    Mapping composition uses one observed document and retires unchanged obsolete
    entries before applying its merge policy. The callback must preserve its
    original file-existence decision for skip-existing.
    """
    destination = native_path(destination)
    previous = claims if claims is not None else invocation.prior.claims if invocation.prior else ()
    old = tuple(claim for claim in previous if claim.role == MCP_CLAIM_ROLE)
    desired = {
        mcp_native_name(item): item
        for item in invocation.artifacts.items()
        if item.content.type == ArtifactType.MCP_SERVER
    }
    if len(desired) != sum(item.content.type == ArtifactType.MCP_SERVER for item in invocation.artifacts.items()):
        raise ConfigError("MCP server native names collide")
    if not desired and old:
        destinations = {claim.destination for claim in old}
        if len(destinations) == 1:
            destination = destinations.pop()
    if any(claim.destination != destination for claim in old):
        raise StateError("MCP native config destination changed; clean up the previous destination first")
    if not desired and not old and map_document is None:
        return ArtifactApplication()
    format: SettingsFormat = "json" if tool == "claude" else "toml"
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    group = invocation.linux_group if isinstance(invocation, WorkspaceSetupInvocation) else ""

    def unavailable() -> ArtifactApplication:
        reason = "Native MCP configuration could not be read or safely updated; review it before reinitializing."
        if old:
            invocation.report_incomplete(reason)
        return ArtifactApplication(
            deferred=tuple(
                ArtifactDeferral(input_id=item.identity, destination="session", reason=reason)
                for item in desired.values()
            ),
            shared_paths=_shared_paths(invocation, old),
        )

    with NativeFiles(invocation.runner) as files:
        if not desired and not old:
            initial = files.read(destination)
            content, _ = map_document(initial) if map_document is not None else (initial, {})
            if content is not None and content != initial:
                files.publish(
                    destination,
                    content,
                    expected=None if initial is None else hashlib.sha256(initial).hexdigest(),
                    group=group,
                    preserve_metadata=initial is not None,
                )
            return ArtifactApplication()
        try:
            initial = files.read(destination)
            document = {} if initial is None else parse_settings(initial, format=format)
            current = dict(_table(document, key))
        except (ConfigError, ConnectivityError, ExternalError):
            return unavailable()

        retained: list[NativeClaim] = []
        retired: list[str] = []
        for claim in old:
            if claim.identifier in desired or claim.source in invocation.retain_artifact_origins:
                retained.append(claim)
            elif claim.identifier not in current:
                continue
            elif claim.sha256 == _fingerprint(current[claim.identifier]):
                _table(document, key).pop(claim.identifier)
                retired.append(claim.identifier)
            else:
                retained.append(claim)
                invocation.report_incomplete(
                    f"Retained modified MCP server '{claim.identifier}'; "
                    "its current native entry differs from applied state."
                )

        working = None if initial is None else serialize_settings(document, format=format)
        contributions: SettingsObject = {}
        if map_document is not None:
            working, contributions = map_document(working)
            document = {} if working is None else parse_settings(working, format=format)
        table = _table(document, key)
        contributed = _table(contributions, key)
        deferred: list[ArtifactDeferral] = []
        confirmed = {claim.identifier: claim for claim in retained}
        for name, item in desired.items():
            entry = render_mcp(item, tool)
            owned_claim = confirmed.get(name)
            observed = table.get(name)
            conflict = name in contributed and observed != entry
            owned = owned_claim is not None and name in current and owned_claim.sha256 == _fingerprint(current[name])
            if conflict or (name in table and observed != entry and not owned):
                reason = (
                    f"MCP server '{name}' conflicts with a mapped or externally managed native entry; "
                    "that entry was retained."
                )
                deferred.append(ArtifactDeferral(input_id=item.identity, destination="session", reason=reason))
                continue
            if key not in document:
                document[key] = table
            table[name] = entry
            confirmed[name] = NativeClaim(
                role=MCP_CLAIM_ROLE,
                identifier=name,
                destination=destination,
                source=item.origin_identity,
                sha256=_fingerprint(entry),
            )

        updated = tuple(claim for claim in previous if claim.role != MCP_CLAIM_ROLE) + tuple(confirmed.values())
        content = serialize_settings(document, format=format)
        if initial is not None and document == parse_settings(initial, format=format):
            content = initial
        try:
            if content != initial and (initial is not None or document):
                files.publish(
                    destination,
                    content,
                    expected=None if initial is None else hashlib.sha256(initial).hexdigest(),
                    group=group,
                    preserve_metadata=initial is not None,
                )
        except (ConnectivityError, ExternalError):
            return unavailable()
        # Persistence failures must escape: a confirmed write cannot be treated as
        # an optional artifact skip or followed by more native mutations.
        if updated != previous:
            invocation.checkpoint(updated)
        for name in retired:
            output.info(
                f"Removed MCP server '{name}'. Any native OAuth credentials are retained. "
                "Use the harness's native logout before withdrawing a server when logout requires its definition."
            )
        return ArtifactApplication(deferred=tuple(deferred), shared_paths=_shared_paths(invocation, updated))
