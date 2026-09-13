"""Codex artifact delivery through native skills, roles, and additive instructions."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import tomli_w

from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral, ArtifactFile
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import (
    NativeSessionArtifacts,
    artifact_file,
    context_text,
    has_artifacts,
    persona_options,
    reject_flags,
    skill_files,
    validate_ancestor_names,
    validate_names,
)
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.artifacts.application import SessionArtifactContext
    from agentworks.artifacts.model import ArtifactInput


_OPTIONS = {"model": str, "model_reasoning_effort": str}


def _persona(item: ArtifactInput) -> str:
    return tomli_w.dumps(
        {
            "name": item.content.name,
            "description": item.content.description,
            "developer_instructions": item.content.text,
            **persona_options(item, "codex", _OPTIONS),
        }
    )


def outer_artifacts(inputs: tuple[ArtifactInput, ...], *, skills_root: str, agents_root: str) -> ArtifactApplication:
    validate_names(inputs)
    files: list[ArtifactFile] = []
    deferred = []
    for item in inputs:
        content = item.content
        if content.type is ArtifactType.SKILL:
            files.extend(skill_files(skills_root, item))
        elif content.type is ArtifactType.AGENT:
            files.append(
                artifact_file(
                    f"{agents_root}/{content.name}.toml", _persona(item), (item,), identity=f"agent:{content.name}"
                )
            )
        else:
            deferred.append(
                ArtifactDeferral(
                    input_id=item.identity,
                    destination="session",
                    reason="Codex loads additive artifact guidance through session configuration",
                )
            )
    return ArtifactApplication(tuple(files), tuple(deferred))


def _reject_overrides(extra_args: Sequence[str]) -> None:
    reject_flags(extra_args, {"--disable", "--profile", "-p"}, "codex")
    for index, token in enumerate(extra_args):
        value = ""
        if token in ("-c", "--config") and index + 1 < len(extra_args):
            value = extra_args[index + 1]
        elif token.startswith("--config="):
            value = token.removeprefix("--config=")
        elif token.startswith("-c"):
            value = token[2:]
        key = value.split("=", 1)[0].strip().strip('"')
        if key in (
            "developer_instructions",
            "model_instructions_file",
            "agents",
            "skills",
            "features",
        ) or key.startswith(("agents.", "skills.", "features.")):
            raise ConfigError(f"codex extra_args overrides artifact delivery configuration: {key}")


def session_artifacts(
    context: SessionArtifactContext | None,
    *,
    configured: str | None,
    extra_args: Sequence[str],
) -> NativeSessionArtifacts:
    if context is None or not has_artifacts(context):
        return NativeSessionArtifacts()
    validate_names(context.inputs)
    _reject_overrides(extra_args)
    files: list[ArtifactFile] = []
    argv: list[str] = []
    deferred = []
    guidance = tuple(item for item in context.inputs if item.content.type in (ArtifactType.HINT, ArtifactType.RULE))
    if guidance:
        text = context_text(guidance, configured)
        files.append(artifact_file(f"{context.directory}/instructions.md", text, guidance))
        argv += ["-c", f"developer_instructions={json.dumps(text, ensure_ascii=False)}"]
    for item in context.inputs:
        if item.content.type is ArtifactType.SKILL:
            deferred.append(
                ArtifactDeferral(
                    input_id=item.identity,
                    destination="session",
                    reason="Codex has no supported private session skill discovery directory",
                )
            )
        elif item.content.type is ArtifactType.AGENT:
            path = f"{context.directory}/agents/{item.content.name}.toml"
            files.append(artifact_file(path, _persona(item), (item,), identity=f"agent:{item.content.name}"))
            key = f"agents.{json.dumps(item.content.name)}"
            argv += [
                "-c",
                f"{key}.config_file={json.dumps(path)}",
                "-c",
                f"{key}.description={json.dumps(item.content.description, ensure_ascii=False)}",
            ]
    application = ArtifactApplication(tuple(files), tuple(deferred))
    validate_ancestor_names(context, application)
    return NativeSessionArtifacts(application, tuple(argv))
