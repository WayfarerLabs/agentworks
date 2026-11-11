"""Codex artifact delivery through native skills, roles, and additive instructions."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

import tomli_w

from agentworks import output
from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.names import mcp_native_name
from agentworks.artifacts.native.common import (
    MAX_CODEX_PERSONA_BYTES,
    NativeSessionArtifacts,
    ancestor_diagnostics,
    artifact_file,
    has_artifacts,
    persona_options,
    reject_flags,
    select_inputs,
    select_session_artifacts,
    skill_files,
    validate_names,
    validate_native_argv,
    validate_native_command,
)
from agentworks.artifacts.native.guidance import guidance_skills, session_skill_files
from agentworks.artifacts.native.mcp import render_mcp
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from agentworks.artifacts.application import SessionArtifactContext
    from agentworks.artifacts.model import ArtifactInput, ArtifactInputs


_OPTIONS = {"model": str, "model_reasoning_effort": str}


_SESSION_WORKAROUNDS = {
    ArtifactType.HINT: "session-home-skills",
    ArtifactType.RULE: "session-home-skills",
    ArtifactType.SKILL: "session-home-skills",
    ArtifactType.AGENT: "session-agent-config",
    ArtifactType.MCP_SERVER: "session-mcp-overrides",
}


def _persona(item: ArtifactInput, *, role_layer: bool = False) -> str:
    values: dict[str, object] = {
        "developer_instructions": item.content.text,
        **persona_options(item, "codex", _OPTIONS),
    }
    if not role_layer:
        values.update(name=item.content.name, description=item.content.description)
    rendered = tomli_w.dumps(values)
    if len(rendered.encode()) > MAX_CODEX_PERSONA_BYTES:
        raise ConfigError("Codex persona exceeds the Agentworks rendered TOML size budget")
    return rendered


def outer_artifacts(inputs: ArtifactInputs, *, skills_root: str, agents_root: str) -> ArtifactApplication:
    validate_names(inputs)
    files = list(guidance_skills(skills_root, inputs))
    for item in inputs.items():
        content = item.content
        if content.type is ArtifactType.SKILL:
            files.extend(skill_files(skills_root, item))
        elif content.type is ArtifactType.AGENT:
            files.append(
                artifact_file(
                    f"{agents_root}/{content.name}.toml", _persona(item), (item,), identity=f"agent:{content.name}"
                )
            )
    return ArtifactApplication(tuple(files))


def vm_artifacts(inputs: ArtifactInputs) -> ArtifactApplication:
    """Use machine skill discovery; other types need native user placement here."""
    validate_names(inputs)
    files = list(guidance_skills("/etc/codex/skills", inputs))
    deferred = []
    for item in inputs.items():
        if item.content.type is ArtifactType.SKILL:
            files.extend(skill_files("/etc/codex/skills", item))
        elif item.content.type in (ArtifactType.AGENT, ArtifactType.MCP_SERVER):
            deferred.append(
                ArtifactDeferral(
                    input_id=item.identity,
                    destination="user",
                    reason=(
                        "This integration has no additive machine-wide location for "
                        f"Codex {item.content.type.map_name}; using native user placement"
                    ),
                )
            )
    return ArtifactApplication(tuple(files), tuple(deferred))


def _reject_overrides(extra_args: Sequence[str], *, skills: bool, agents: bool) -> None:
    reject_flags(extra_args, {"--profile", "-p"}, "codex")
    features = ({"multi_agent"} if agents else set()) | ({"skills"} if skills else set())
    reserved = {"features"} if features else set()
    if agents:
        reserved.add("agents")
    if skills:
        reserved.add("skills")
    for index, token in enumerate(extra_args):
        if token == "--disable" and index + 1 < len(extra_args) and extra_args[index + 1] in features:
            raise ConfigError("codex extra_args disables native artifact discovery")
        if token.startswith("--disable=") and token.split("=", 1)[1] in features:
            raise ConfigError("codex extra_args disables native artifact discovery")
        value = ""
        if token in ("-c", "--config") and index + 1 < len(extra_args):
            value = extra_args[index + 1]
        elif token.startswith("--config="):
            value = token.removeprefix("--config=")
        elif token.startswith("-c"):
            value = token[2:]
        key = value.split("=", 1)[0].strip().strip('"')
        if key in reserved or any(key.startswith(name + ".") for name in reserved):
            raise ConfigError(f"codex extra_args overrides artifact delivery configuration: {key}")


def session_artifacts(
    context: SessionArtifactContext | None,
    *,
    extra_args: Sequence[str],
    enabled_workarounds: Sequence[str] = (),
    profile: str | None = None,
    mcp_conflicts: Callable[[ArtifactInputs, SessionArtifactContext], Mapping[str, str]] | None = None,
) -> NativeSessionArtifacts:
    """Render session inputs, validating persona names for native override paths."""
    if context is None or not has_artifacts(context):
        return NativeSessionArtifacts()
    inputs, deferred = select_session_artifacts(context.inputs, enabled_workarounds, _SESSION_WORKAROUNDS)
    mcp_inputs = select_inputs(
        inputs, {item.identity for item in inputs.items() if item.content.type is ArtifactType.MCP_SERVER}
    )
    inputs = select_inputs(
        inputs, {item.identity for item in inputs.items() if item.content.type is not ArtifactType.MCP_SERVER}
    )
    mcp = _session_mcp(context, mcp_inputs, extra_args, mcp_conflicts, profile=profile)
    deferred = (*deferred, *mcp.application.deferred)
    validate_names(inputs, types=(ArtifactType.AGENT,))
    types = {item.content.type for item in inputs.items()}
    ancestors = {file.native_identity or "" for file in context.ancestor_files}
    if inputs:
        _reject_overrides(
            extra_args,
            skills=bool(types & {ArtifactType.HINT, ArtifactType.RULE, ArtifactType.SKILL}),
            agents=ArtifactType.AGENT in types,
        )
    if context.ancestor_files:
        with ancestor_diagnostics("codex"):
            _reject_overrides(
                extra_args,
                skills=any(name.startswith("skill:") for name in ancestors),
                agents=any(name.startswith("agent:") for name in ancestors),
            )

    skills_root = f"{context.home}/.agents/skills"
    files = list(guidance_skills(skills_root, inputs, context=context))
    argv: list[str] = []
    for item in inputs.items():
        if item.content.type is ArtifactType.SKILL:
            files.extend(session_skill_files(skills_root, item, context))
    if files:
        output.warn(
            "Codex session-home-skills uses the shared user skill catalog. "
            "Session/run applicability is best effort; other sessions can discover these entries."
        )
    for item in inputs.items():
        if item.content.type is ArtifactType.AGENT:
            name = item.content.name
            path = f"{context.directory}/agents/{item.content.name}.toml"
            files.append(
                artifact_file(path, _persona(item, role_layer=True), (item,), identity=f"agent:{item.content.name}")
            )
            # Codex CLI override keys are dotted paths with literal components, including quotes.
            key = f"agents.{name}"
            argv += [
                "-c",
                f"{key}.config_file={json.dumps(path)}",
                "-c",
                f"{key}.description={json.dumps(item.content.description, ensure_ascii=False)}",
            ]
    try:
        validate_native_argv((*argv, *mcp.argv))
        validate_native_command(shlex.quote(shlex.join((*argv, *mcp.argv))))
    except ConfigError:
        # Optional MCP must not prevent an otherwise-valid legacy launch.
        validate_native_argv(tuple(argv))
        deferred = (
            *deferred,
            *(
                ArtifactDeferral(
                    input_id=item.identity,
                    destination="session",
                    reason="session-mcp-overrides exceeds the native command carrier limit",
                )
                for item in mcp_inputs.items()
                if item.identity not in {entry.input_id for entry in mcp.application.deferred}
            ),
        )
    else:
        argv.extend(mcp.argv)
    application = ArtifactApplication(tuple(files), deferred)
    return NativeSessionArtifacts(application, tuple(argv))


def _config_values(extra_args: Sequence[str]) -> list[str]:
    values = []
    for index, token in enumerate(extra_args):
        if token in ("-c", "--config") and index + 1 < len(extra_args):
            values.append(extra_args[index + 1])
        elif token.startswith("--config="):
            values.append(token.removeprefix("--config="))
        elif token.startswith("-c"):
            values.append(token[2:])
    return values


def _mcp_override_conflicts(extra_args: Sequence[str], names: set[str]) -> set[str]:
    """Match native CLI dotted paths; its components are literal, not TOML keys."""
    conflicts: set[str] = set()
    for value in _config_values(extra_args):
        path = value.split("=", 1)[0].strip().split(".")
        if path[0] == "mcp_servers":
            conflicts.update(names if len(path) == 1 else names.intersection({path[1]}))
    return conflicts


def _toml_literal(value: object) -> str:
    if isinstance(value, dict):
        # CLI paths split on dots, so header/env maps travel as one leaf table.
        return (
            "{"
            + ", ".join(
                f"{json.dumps(key, ensure_ascii=False)} = {_toml_literal(entry)}" for key, entry in value.items()
            )
            + "}"
        )
    return tomli_w.dumps({"value": value}).split("=", 1)[1].strip()


def _session_mcp(
    context: SessionArtifactContext,
    inputs: ArtifactInputs,
    extra_args: Sequence[str],
    inspect: Callable[[ArtifactInputs, SessionArtifactContext], Mapping[str, str]] | None,
    *,
    profile: str | None,
) -> NativeSessionArtifacts:
    items = tuple(inputs.items())
    if profile is not None or any(
        token.split("=", 1)[0] in {"--profile", "-p"} or (token.startswith("-p") and not token.startswith("--"))
        for token in extra_args
    ):
        return NativeSessionArtifacts(
            ArtifactApplication(
                deferred=tuple(
                    ArtifactDeferral(
                        input_id=item.identity,
                        destination="session",
                        reason=(
                            "session-mcp-overrides does not inspect explicit Codex profiles; "
                            "use native user/workspace MCP placement."
                        ),
                    )
                    for item in items
                )
            )
        )
    conflicts = _mcp_override_conflicts(extra_args, {mcp_native_name(item) for item in items})
    inspect_inputs = select_inputs(inputs, {item.identity for item in items if mcp_native_name(item) not in conflicts})
    native_conflicts = {} if inspect is None or not inspect_inputs else inspect(inspect_inputs, context)
    deferred = []
    argv: list[str] = []
    for item in items:
        name = mcp_native_name(item)
        if name in conflicts:
            deferred.append(
                ArtifactDeferral(
                    input_id=item.identity,
                    destination="session",
                    reason="session-mcp-overrides conflicts with an extra_args MCP override",
                )
            )
            continue
        if item.identity in native_conflicts:
            deferred.append(
                ArtifactDeferral(input_id=item.identity, destination="session", reason=native_conflicts[item.identity])
            )
            continue
        try:
            argv.extend(("-c", f"mcp_servers.{name}={_toml_literal(render_mcp(item, 'codex'))}"))
        except ConfigError as error:
            deferred.append(ArtifactDeferral(input_id=item.identity, destination="session", reason=str(error)))
    return NativeSessionArtifacts(ArtifactApplication(deferred=tuple(deferred)), tuple(argv))
