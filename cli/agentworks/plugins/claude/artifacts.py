"""Claude Code's native artifact files and session-only carriers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import yaml

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import (
    NativeSessionArtifacts,
    artifact_file,
    context_text,
    has_artifacts,
    json_text,
    persona_options,
    reject_flags,
    skill_files,
    validate_ancestor_names,
    validate_names,
    validate_native_argv,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.artifacts.application import SessionArtifactContext
    from agentworks.artifacts.model import ArtifactInput, ArtifactInputs


_OPTIONS = {"model": str, "tools": list, "disallowedTools": list, "maxTurns": int}


def _persona(item: ArtifactInput) -> dict[str, object]:
    return {
        "description": item.content.description,
        "prompt": item.content.text,
        **persona_options(item, "claude-code", _OPTIONS),
    }


def outer_artifacts(inputs: ArtifactInputs, root: str) -> ArtifactApplication:
    validate_names(inputs)
    files = []
    hints = tuple(item for item in inputs.items() if item.content.type is ArtifactType.HINT)
    if hints:
        files.append(
            artifact_file(f"{root}/rules/agentworks-hints.md", "# Agentworks setup\n\n" + context_text(hints), hints)
        )
    rules: dict[str, list[ArtifactInput]] = {}
    for group in inputs.groups():
        for name, item in group.rules.items():
            rules.setdefault(name, []).append(item)
    for name, contributions in rules.items():
        files.append(
            artifact_file(
                f"{root}/rules/agentworks-rule-{name}.md",
                f"# {name}\n\n" + context_text(tuple(contributions)),
                tuple(contributions),
            )
        )
    for item in inputs.items():
        content = item.content
        if content.type is ArtifactType.SKILL:
            files.extend(skill_files(f"{root}/skills", item))
        elif content.type is ArtifactType.AGENT:
            options = _persona(item)
            del options["prompt"]
            header = yaml.safe_dump({"name": content.name, **options}, sort_keys=False)
            files.append(
                artifact_file(
                    f"{root}/agents/{content.name}.md",
                    f"---\n{header}---\n{content.text}",
                    (item,),
                    identity=f"agent:{content.name}",
                )
            )
    return ArtifactApplication(tuple(files))


def session_artifacts(
    context: SessionArtifactContext | None,
    *,
    configured: str | None,
    extra_args: Sequence[str],
) -> NativeSessionArtifacts:
    if context is None or not has_artifacts(context):
        return NativeSessionArtifacts()
    inputs = context.inputs
    validate_names(inputs)
    reject_flags(
        extra_args,
        {
            "--append-system-prompt",
            "--append-system-prompt-file",
            "--system-prompt",
            "--system-prompt-file",
            "--system-prompt-snapshot",
            "--agents",
            "--plugin-dir",
            "--disable-slash-commands",
            "--bare",
            "--settings",
            "--setting-sources",
        },
        "claude-code",
    )
    files = []
    argv: list[str] = []
    guidance = tuple(item for item in inputs.items() if item.content.type in (ArtifactType.HINT, ArtifactType.RULE))
    if guidance:
        path = f"{context.directory}/instructions.md"
        files.append(artifact_file(path, context_text(guidance, configured), guidance))
        argv += ["--append-system-prompt-file", path]
    if guidance or any("/rules/" in file.path for file in context.ancestor_files):
        argv += ["--system-prompt-snapshot", "off"]
    skills = tuple(item for item in inputs.items() if item.content.type is ArtifactType.SKILL)
    if skills:
        plugin = f"{context.directory}/plugin"
        files.append(
            artifact_file(
                f"{plugin}/.claude-plugin/plugin.json",
                json_text({"name": "agentworks-artifacts", "version": "1.0.0"}),
                skills,
            )
        )
        for item in skills:
            files.extend(skill_files(f"{plugin}/skills", item, namespace="agentworks-artifacts:"))
        argv += ["--plugin-dir", plugin]
    agents = tuple(item for item in inputs.items() if item.content.type is ArtifactType.AGENT)
    if agents:
        value = {item.content.name: _persona(item) for item in agents}
        for item in agents:
            files.append(
                artifact_file(
                    f"{context.directory}/agents/{item.content.name}.json",
                    json_text(value[item.content.name]),
                    (item,),
                    identity=f"agent:{item.content.name}",
                )
            )
        argv += ["--agents", json.dumps(value, ensure_ascii=False)]
    application = ArtifactApplication(tuple(files))
    validate_ancestor_names(context, application)
    validate_native_argv(tuple(argv))
    return NativeSessionArtifacts(
        application,
        tuple(argv),
        tuple(
            flag
            for flag in ("--append-system-prompt-file", "--system-prompt-snapshot", "--plugin-dir", "--agents")
            if flag in argv
        ),
    )
