"""Static MCP translation and entry ownership within shared native documents."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, cast

from agentworks import output
from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral
from agentworks.artifacts.model import ArtifactInput, ArtifactInputs, ArtifactType
from agentworks.artifacts.names import mcp_native_name
from agentworks.artifacts.native.layout import NativeUserLayout, resolve_user_layout
from agentworks.capabilities.harness_integration.settings import (
    SettingsFormat,
    SettingsObject,
    SettingsValue,
    parse_settings,
    serialize_settings,
)
from agentworks.capabilities.harness_integration.setup import UserSetupInvocation, WorkspaceSetupInvocation
from agentworks.errors import ConfigError, ConnectivityError, ExternalError
from agentworks.execution.errors import PathRefused
from agentworks.execution.files import guest_path
from agentworks.harness_setup.model import NativeClaim

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.execution.access import VMAccess

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


def _user_mcp_path(layout: NativeUserLayout, home: str) -> str:
    if not layout.mcp_path.startswith(guest_path(home).rstrip("/") + "/"):
        raise ConfigError("User MCP config placement outside HOME is unsupported")
    return layout.mcp_path


def resolve_mcp_user_path(tool: MCPTool, access: VMAccess, *, home: str, environment: Mapping[str, str]) -> str:
    """Observe the native layout and restrict MCP placement to the actual user's HOME."""
    return _user_mcp_path(resolve_user_layout(tool, access, home=home, environment=environment), home)


def user_mcp_location(
    layout: NativeUserLayout, invocation: UserSetupInvocation, *, retiring: bool
) -> tuple[str | None, bool, tuple[ArtifactDeferral, ...]]:
    """Resolve current-role authority, preserving prior claims when it is unavailable."""
    items = tuple(item for item in invocation.artifacts.items() if item.content.type is ArtifactType.MCP_SERVER)
    previous = (
        tuple(claim for claim in invocation.prior.claims if claim.role == MCP_CLAIM_ROLE) if invocation.prior else ()
    )
    try:
        return _user_mcp_path(layout, invocation.home), False, ()
    except ConfigError:
        reason = "Native MCP config location could not be resolved safely; existing MCP ownership is retained."
        if previous:
            invocation.report_incomplete(reason)
        return (
            None,
            True,
            (
                ()
                if retiring
                else tuple(
                    ArtifactDeferral(input_id=item.identity, destination="session", reason=reason) for item in items
                )
            ),
        )


def session_mcp_conflicts(
    tool: Literal["claude", "codex"],
    access: VMAccess | None,
    inputs: ArtifactInputs,
    *,
    home: str,
    environment: Mapping[str, str],
    workspace: str,
) -> dict[str, str]:
    """Read fixed native roles before optional invocation overrides; never return config values."""
    items = tuple(item for item in inputs.items() if item.content.type is ArtifactType.MCP_SERVER)
    if not items:
        return {}
    conflicts: dict[str, str] = {}
    if access is None:
        return {
            item.identity: "The native launch target is unavailable for MCP configuration checks." for item in items
        }
    try:
        user = resolve_mcp_user_path(tool, access, home=home, environment=environment)
        project = guest_path(workspace) + ("/.mcp.json" if tool == "claude" else "/.codex/config.toml")
        key = "mcpServers" if tool == "claude" else "mcp_servers"
        format: SettingsFormat = "json" if tool == "claude" else "toml"
        files = access.files()
        for path in dict.fromkeys((user, project)):
            content = files.read(path)
            document = {} if content is None else parse_settings(content, format=format)
            tables = [_table(document, key)]
            if tool == "claude" and path == user:
                projects = _table(document, "projects")
                local = projects.get(workspace, {})
                if not isinstance(local, dict):
                    raise ConfigError("native MCP project config must use an object")
                tables.append(_table(local, key))
            for item in items:
                name = mcp_native_name(item)
                expected = render_mcp(item, tool)
                if any(name in table and table[name] != expected for table in tables):
                    conflicts[item.identity] = (
                        "An existing native MCP entry differs; invocation delivery leaves it unchanged."
                    )
    except (ConfigError, ConnectivityError, ExternalError, PathRefused):
        return {
            item.identity: "Native MCP configuration could not be checked safely; invocation delivery is deferred."
            for item in items
        }
    return conflicts


def _fingerprint(value: SettingsValue) -> str:
    content = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(content).hexdigest()


def _matches(claim: NativeClaim, value: SettingsValue) -> bool:
    if claim.sha256 is None:
        return False
    try:
        return claim.sha256 == _fingerprint(value)
    except (TypeError, ValueError):
        # TOML dates/times and other non-JSON edits cannot match a managed
        # canonical entry, even when their printed text resembles a prior string.
        return False


def _table(document: SettingsObject, key: str) -> SettingsObject:
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError("native MCP servers must use an object or table")
    return value


def _shared_paths(
    invocation: UserSetupInvocation | WorkspaceSetupInvocation, claims: tuple[NativeClaim, ...], *, destination: str
) -> tuple[str, ...]:
    if isinstance(invocation, WorkspaceSetupInvocation) and any(
        claim.role == MCP_CLAIM_ROLE and claim.destination == destination for claim in claims
    ):
        return (destination,)
    return ()


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
    destination = guest_path(destination)
    previous = claims if claims is not None else invocation.prior.claims if invocation.prior else ()
    old = tuple(claim for claim in previous if claim.role == MCP_CLAIM_ROLE)
    desired = {
        mcp_native_name(item): item
        for item in invocation.artifacts.items()
        if item.content.type == ArtifactType.MCP_SERVER
    }
    elsewhere = tuple(claim for claim in old if claim.destination != destination)
    old = tuple(claim for claim in old if claim.destination == destination)
    blocked = {
        name: item
        for name, item in desired.items()
        if any(claim.identifier == name or claim.source == item.origin_identity for claim in elsewhere)
    }
    retained_location_reason = (
        "Recorded MCP ownership belongs to another config location; its entries are retained for explicit cleanup."
    )
    if elsewhere:
        invocation.report_incomplete(retained_location_reason)
    blocked_deferrals = tuple(
        ArtifactDeferral(input_id=item.identity, destination="session", reason=retained_location_reason)
        for item in blocked.values()
    )
    desired = {name: item for name, item in desired.items() if name not in blocked}
    if not desired and not old and map_document is None:
        return ArtifactApplication(deferred=blocked_deferrals)
    format: SettingsFormat = "json" if tool == "claude" else "toml"
    key = "mcpServers" if tool == "claude" else "mcp_servers"
    group = (invocation.linux_group or None) if isinstance(invocation, WorkspaceSetupInvocation) else None
    files = invocation.files

    def publish(content: bytes, initial: bytes | None) -> None:
        """Write ``content`` over ``initial``, keeping an existing file's owner, mode and attributes.

        A new file takes the modes settings publication uses: 0600 under 0700
        parents for a user, 0660 under setgid 2770 parents for a workspace.
        """
        files.update(
            destination,
            content,
            observed=initial,
            mode=0o600 if group is None else 0o660,
            parent_mode=0o700 if group is None else 0o2770,
            group=group,
            preserve_metadata=initial is not None,
        )

    def unavailable() -> ArtifactApplication:
        reason = "Native MCP configuration could not be read or safely updated; review it before reinitializing."
        if old or map_document is not None:
            invocation.report_incomplete(reason)
        return ArtifactApplication(
            deferred=(
                *blocked_deferrals,
                *tuple(
                    ArtifactDeferral(input_id=item.identity, destination="session", reason=reason)
                    for item in desired.values()
                ),
            ),
            shared_paths=_shared_paths(invocation, old, destination=destination),
        )

    if not desired and not old:
        initial = files.read(destination)
        content, _ = map_document(initial) if map_document is not None else (initial, {})
        if content is not None and content != initial:
            publish(content, initial)
        return ArtifactApplication(deferred=blocked_deferrals)
    try:
        initial = files.read(destination)
    except (ConnectivityError, ExternalError, PathRefused):
        return unavailable()
    mapped = False
    contributions: SettingsObject = {}
    try:
        document = {} if initial is None else parse_settings(initial, format=format)
        current = dict(_table(document, key))
        original_document = {} if initial is None else parse_settings(initial, format=format)
    except ConfigError:
        if map_document is None:
            return unavailable()
        # An explicit replacement retains its repair behavior. Ownership of
        # unreadable old entries cannot authorize changes to source entries.
        replacement, contributions = map_document(initial)
        try:
            document = {} if replacement is None else parse_settings(replacement, format=format)
            _table(document, key)
        except ConfigError:
            return unavailable()
        current = {}
        original_document = None
        mapped = True

    retained: list[NativeClaim] = []
    retired: list[str] = []
    absent: set[str] = set()
    modified: list[NativeClaim] = []
    for claim in old:
        if claim.identifier in desired or claim.source in invocation.retain_artifact_origins:
            retained.append(claim)
        elif claim.identifier not in current:
            retired.append(claim.identifier)
            absent.add(claim.identifier)
            continue
        elif _matches(claim, current[claim.identifier]):
            _table(document, key).pop(claim.identifier)
            retired.append(claim.identifier)
        else:
            modified.append(claim)

    working = None if initial is None else serialize_settings(document, format=format)
    if map_document is not None and not mapped:
        working, contributions = map_document(working)
        document = {} if working is None else parse_settings(working, format=format)
    table = _table(document, key)
    contributed = _table(contributions, key)
    for claim in modified:
        # The explicit settings policy authorizes takeover or removal here;
        # MCP retirement alone cannot change an externally modified entry.
        if map_document is not None and (claim.identifier in contributed or claim.identifier not in table):
            retired.append(claim.identifier)
        else:
            retained.append(claim)
            invocation.report_incomplete(
                f"Retained modified MCP server '{claim.identifier}'; "
                "its current native entry differs from applied state."
            )
    deferred: list[ArtifactDeferral] = list(blocked_deferrals)
    confirmed = {claim.identifier: claim for claim in retained}
    for name, item in desired.items():
        entry = render_mcp(item, tool)
        owned_claim = confirmed.get(name)
        observed = table.get(name)
        conflict = name in contributed and observed != entry
        owned = owned_claim is not None and name in current and _matches(owned_claim, current[name])
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

    updated = tuple(claim for claim in previous if claim.role != MCP_CLAIM_ROLE) + elsewhere + tuple(confirmed.values())
    content = serialize_settings(document, format=format)
    if initial is not None and document == original_document:
        content = initial
    try:
        if content != initial and (initial is not None or document):
            publish(content, initial)
    except (ConnectivityError, ExternalError, PathRefused):
        return unavailable()
    # Persistence failures must escape: a confirmed write cannot be treated as
    # an optional artifact skip or followed by more native mutations.
    if updated != previous:
        invocation.checkpoint(updated)
    for name in retired:
        action = (
            "Released MCP ownership of native server"
            if name in table
            else "Cleared ownership of already absent MCP server"
            if name in absent
            else "Removed MCP server"
        )
        output.info(
            f"{action} '{name}'. Any native OAuth credentials are retained. "
            "Use the harness's native logout before withdrawing a server when logout requires its definition."
        )
    return ArtifactApplication(
        deferred=tuple(deferred), shared_paths=_shared_paths(invocation, updated, destination=destination)
    )
