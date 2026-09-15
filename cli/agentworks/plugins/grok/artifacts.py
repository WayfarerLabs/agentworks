"""Grok Build artifact placement for its interactive native discovery contract."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import yaml

from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral
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


# Only options represented by the interactive JSON and Markdown agent surfaces.
_OPTIONS = {"model": str, "tools": list}


def _persona(item: ArtifactInput) -> dict[str, object]:
    return {
        "description": item.content.description,
        "prompt": item.content.text,
        **persona_options(item, "grok-build", _OPTIONS),
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
    validate_names(context.inputs)
    reject_flags(
        extra_args,
        {
            "--rules",
            "--append-system-prompt",
            "--system-prompt",
            "--system-prompt-override",
            "--agents",
            "--no-subagents",
            "--plugin-dir",
            "--disable-skills",
        },
        "grok-build",
    )
    files = []
    argv: list[str] = []
    deferred = []
    guidance = tuple(
        item for item in context.inputs.items() if item.content.type in (ArtifactType.HINT, ArtifactType.RULE)
    )
    if guidance:
        text = context_text(guidance, configured)
        argv += ["--rules", text]
    agents = tuple(item for item in context.inputs.items() if item.content.type is ArtifactType.AGENT)
    if agents:
        definitions = {item.content.name: _persona(item) for item in agents}
        for item in agents:
            files.append(
                artifact_file(
                    f"{context.directory}/agents/{item.content.name}.json",
                    json_text(definitions[item.content.name]),
                    (item,),
                    identity=f"agent:{item.content.name}",
                )
            )
        argv += ["--agents", json.dumps(definitions, ensure_ascii=False)]
    for item in context.inputs.items():
        if item.content.type is ArtifactType.SKILL:
            deferred.append(
                ArtifactDeferral(
                    input_id=item.identity,
                    destination="session",
                    reason="Grok Build's interactive CLI has no supported private skill directory",
                )
            )
    application = ArtifactApplication(tuple(files), tuple(deferred))
    validate_ancestor_names(context, application)
    validate_native_argv(tuple(argv))
    return NativeSessionArtifacts(
        application, tuple(argv), (("--rules",) if guidance else ()) + (("--agents",) if agents else ())
    )
