"""Claude Code's native artifact files and session-only carriers."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import yaml

from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral, ArtifactFile
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.names import mcp_native_name
from agentworks.artifacts.native.common import (
    NativeSessionArtifacts,
    ancestor_diagnostics,
    artifact_file,
    has_artifacts,
    json_text,
    persona_options,
    reject_flags,
    select_inputs,
    select_session_artifacts,
    skill_files,
    validate_names,
    validate_native_argv,
)
from agentworks.artifacts.native.guidance import guidance_rules, guidance_skills
from agentworks.artifacts.native.mcp import render_mcp

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from agentworks.artifacts.application import SessionArtifactContext
    from agentworks.artifacts.model import ArtifactInput, ArtifactInputs


_OPTIONS = {"model": str, "tools": list, "disallowedTools": list, "maxTurns": int}


_SESSION_WORKAROUNDS = {
    ArtifactType.HINT: "session-skill-plugin",
    ArtifactType.RULE: "session-skill-plugin",
    ArtifactType.SKILL: "session-skill-plugin",
    ArtifactType.AGENT: "session-agent-definitions",
    ArtifactType.MCP_SERVER: "session-mcp-config",
}


def _persona(item: ArtifactInput) -> dict[str, object]:
    return {
        "description": item.content.description,
        "prompt": item.content.text,
        **persona_options(item, "claude-code", _OPTIONS),
    }


def _persona_file(root: str, item: ArtifactInput, *, namespace: str = "") -> ArtifactFile:
    options = _persona(item)
    del options["prompt"]
    header = yaml.safe_dump({"name": item.content.name, **options}, sort_keys=False)
    return artifact_file(
        f"{root}/{item.content.name}.md",
        f"---\n{header}---\n{item.content.text}",
        (item,),
        identity=f"agent:{namespace}{item.content.name}",
    )


def outer_artifacts(inputs: ArtifactInputs, root: str, *, guidance_as_skills: bool = False) -> ArtifactApplication:
    validate_names(inputs)
    files = list(
        guidance_skills(f"{root}/skills", inputs) if guidance_as_skills else guidance_rules(f"{root}/rules", inputs)
    )
    for item in inputs.items():
        if item.content.type is ArtifactType.SKILL:
            files.extend(skill_files(f"{root}/skills", item))
        elif item.content.type is ArtifactType.AGENT:
            files.append(_persona_file(f"{root}/agents", item))
    return ArtifactApplication(tuple(files))


def session_artifacts(
    context: SessionArtifactContext | None,
    *,
    extra_args: Sequence[str],
    enabled_workarounds: Sequence[str] = (),
    mcp_conflicts: Callable[[ArtifactInputs, SessionArtifactContext], Mapping[str, str]] | None = None,
) -> NativeSessionArtifacts:
    if context is None or not has_artifacts(context):
        return NativeSessionArtifacts()
    inputs, deferred = select_session_artifacts(context.inputs, enabled_workarounds, _SESSION_WORKAROUNDS)
    mcp_inputs = select_inputs(
        inputs, {item.identity for item in inputs.items() if item.content.type is ArtifactType.MCP_SERVER}
    )
    inputs = select_inputs(
        inputs, {item.identity for item in inputs.items() if item.content.type is not ArtifactType.MCP_SERVER}
    )
    mcp = _session_mcp(context, mcp_inputs, extra_args, mcp_conflicts)
    deferred = (*deferred, *mcp.application.deferred)
    if not inputs and not context.ancestor_files:
        return NativeSessionArtifacts(ArtifactApplication(mcp.application.files, deferred), mcp.argv)
    validate_names(inputs)
    if inputs:
        reject_flags(
            extra_args,
            {"--bare", "--settings", "--setting-sources", "--plugin-dir", "--disable-slash-commands"},
            "claude-code",
        )
    if context.ancestor_files:
        with ancestor_diagnostics("claude-code"):
            reject_flags(
                extra_args, {"--bare", "--settings", "--setting-sources", "--disable-slash-commands"}, "claude-code"
            )
    if not inputs:
        return NativeSessionArtifacts(ArtifactApplication(mcp.application.files, deferred), mcp.argv)
    plugin = f"{context.directory}/plugin"
    plugin_name = (
        "agentworks-"
        + hashlib.sha256(context.session_uuid.encode()).hexdigest()[:12]
        + "-"
        + hashlib.sha256(context.run_id.encode()).hexdigest()[:8]
    )
    items = tuple(inputs.items())
    files = [
        artifact_file(
            f"{plugin}/.claude-plugin/plugin.json", json_text({"name": plugin_name, "version": "1.0.0"}), items
        )
    ]
    files.extend(guidance_skills(f"{plugin}/skills", inputs, namespace=plugin_name + ":"))
    for item in items:
        if item.content.type is ArtifactType.SKILL:
            files.extend(skill_files(f"{plugin}/skills", item, namespace=plugin_name + ":"))
        elif item.content.type is ArtifactType.AGENT:
            files.append(_persona_file(f"{plugin}/agents", item, namespace=plugin_name + ":"))
    application = ArtifactApplication((*files, *mcp.application.files), deferred)
    argv = ("--plugin-dir", plugin, *mcp.argv)
    validate_native_argv(argv)
    return NativeSessionArtifacts(application, argv)


def _session_mcp(
    context: SessionArtifactContext,
    inputs: ArtifactInputs,
    extra_args: Sequence[str],
    inspect: Callable[[ArtifactInputs, SessionArtifactContext], Mapping[str, str]] | None,
) -> NativeSessionArtifacts:
    """Keep opted-in MCP configuration private to this invocation."""
    items = tuple(inputs.items())
    if not items:
        return NativeSessionArtifacts()
    conflicts = {token.split("=", 1)[0] for token in extra_args} & {
        "--bare",
        "--mcp-config",
        "--strict-mcp-config",
        "--",
    }
    if conflicts:
        reason = "session-mcp-config conflicts with extra_args: " + ", ".join(sorted(conflicts))
        return NativeSessionArtifacts(
            ArtifactApplication(
                deferred=tuple(
                    ArtifactDeferral(input_id=item.identity, destination="session", reason=reason) for item in items
                )
            )
        )
    path = f"{context.directory}/mcp.json"
    native_conflicts = {} if inspect is None else inspect(inputs, context)
    servers = {}
    selected = []
    deferred = []
    for item in items:
        if item.identity in native_conflicts:
            deferred.append(
                ArtifactDeferral(input_id=item.identity, destination="session", reason=native_conflicts[item.identity])
            )
            continue
        servers[mcp_native_name(item)] = render_mcp(item, "claude")
        selected.append(item)
    if not selected:
        return NativeSessionArtifacts(ArtifactApplication(deferred=tuple(deferred)))
    file = artifact_file(path, json_text({"mcpServers": servers}), tuple(selected))
    return NativeSessionArtifacts(ArtifactApplication(files=(file,), deferred=tuple(deferred)), ("--mcp-config", path))
