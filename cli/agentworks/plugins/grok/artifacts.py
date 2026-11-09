"""Grok Build native discovery and explicitly enabled session carriers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from agentworks import output
from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import (
    NativeSessionArtifacts,
    ancestor_diagnostics,
    artifact_file,
    has_artifacts,
    native_home,
    persona_options,
    reject_flags,
    select_session_artifacts,
    skill_files,
    validate_ancestor_names,
    validate_names,
)
from agentworks.artifacts.native.guidance import guidance_rules, guidance_skills, session_skill_files

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.artifacts.application import SessionArtifactContext
    from agentworks.artifacts.model import ArtifactInput, ArtifactInputs


# Only options represented by the native Markdown agent surface.
_OPTIONS = {"model": str, "tools": list}


_SESSION_WORKAROUNDS = {
    ArtifactType.HINT: "session-home-skills",
    ArtifactType.RULE: "session-home-skills",
    ArtifactType.SKILL: "session-home-skills",
}


def _persona(item: ArtifactInput) -> dict[str, object]:
    return {
        "description": item.content.description,
        "prompt": item.content.text,
        **persona_options(item, "grok-build", _OPTIONS),
    }


def outer_artifacts(inputs: ArtifactInputs, root: str, *, workspace: bool = False) -> ArtifactApplication:
    validate_names(inputs)
    files = list(guidance_skills(f"{root}/skills", inputs) if workspace else guidance_rules(f"{root}/rules", inputs))
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
    extra_args: Sequence[str],
    enabled_workarounds: Sequence[str] = (),
) -> NativeSessionArtifacts:
    if context is None or not has_artifacts(context):
        return NativeSessionArtifacts()
    inputs, deferred = select_session_artifacts(context.inputs, enabled_workarounds, _SESSION_WORKAROUNDS)
    if not inputs and not context.ancestor_files:
        return NativeSessionArtifacts(ArtifactApplication(deferred=deferred))
    selected_types = {item.content.type for item in inputs.items()}
    ancestor_names = {file.native_identity or "" for file in context.ancestor_files}
    if selected_types & {ArtifactType.HINT, ArtifactType.RULE, ArtifactType.SKILL}:
        reject_flags(extra_args, {"--disable-skills"}, "grok-build")
    if context.ancestor_files:
        ancestor_flags = set()
        if any(name.startswith("agent:") for name in ancestor_names):
            ancestor_flags.update({"--agents", "--no-subagents"})
        if any(name.startswith("skill:") for name in ancestor_names):
            ancestor_flags.add("--disable-skills")
        with ancestor_diagnostics("grok-build"):
            reject_flags(extra_args, ancestor_flags, "grok-build")
    root = native_home(context.home, context.environment, "GROK_HOME", ".grok")
    files = list(guidance_skills(f"{root}/skills", inputs, context=context))
    for item in inputs.items():
        if item.content.type is ArtifactType.SKILL:
            files.extend(session_skill_files(f"{root}/skills", item, context))
    if files:
        output.warn(
            "Grok session-home-skills uses the shared user skill catalog. "
            "Session/run applicability is best effort; other sessions can discover these entries."
        )
    application = ArtifactApplication(tuple(files), deferred)
    validate_ancestor_names(context, application)
    return NativeSessionArtifacts(application)
