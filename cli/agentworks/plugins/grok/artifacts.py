"""Grok Build native discovery and explicitly enabled session carriers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from agentworks.artifacts.application import ArtifactApplication
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import (
    NativeSessionArtifacts,
    ancestor_diagnostics,
    artifact_file,
    context_text,
    has_artifacts,
    persona_options,
    reject_flags,
    select_session_artifacts,
    skill_files,
    validate_ancestor_names,
    validate_names,
    validate_native_argv,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentworks.artifacts.application import SessionArtifactContext
    from agentworks.artifacts.model import ArtifactInput, ArtifactInputs


# Only options represented by the native Markdown agent surface.
_OPTIONS = {"model": str, "tools": list}


_SESSION_WORKAROUNDS = {
    ArtifactType.HINT: "session-rules",
    ArtifactType.RULE: "session-rules",
}


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
    enabled_workarounds: Sequence[str] = (),
) -> NativeSessionArtifacts:
    if context is None or not has_artifacts(context):
        return NativeSessionArtifacts()
    inputs, deferred = select_session_artifacts(context.inputs, enabled_workarounds, _SESSION_WORKAROUNDS)
    if not inputs and not context.ancestor_files:
        return NativeSessionArtifacts(ArtifactApplication(deferred=deferred))
    validate_names(inputs)
    selected_types = {item.content.type for item in inputs.items()}
    ancestor_names = {file.native_identity or "" for file in context.ancestor_files}
    forbidden = set()
    if selected_types & {ArtifactType.HINT, ArtifactType.RULE}:
        forbidden.update({"--rules", "--append-system-prompt", "--system-prompt", "--system-prompt-override"})
    if inputs:
        reject_flags(extra_args, forbidden, "grok-build")
    if context.ancestor_files:
        ancestor_flags = set()
        if any(name.startswith("agent:") for name in ancestor_names):
            ancestor_flags.update({"--agents", "--no-subagents"})
        if any(name.startswith("skill:") for name in ancestor_names):
            ancestor_flags.update({"--plugin-dir", "--disable-skills"})
        with ancestor_diagnostics("grok-build"):
            reject_flags(extra_args, ancestor_flags, "grok-build")
    argv: list[str] = []
    guidance = tuple(item for item in inputs.items() if item.content.type in (ArtifactType.HINT, ArtifactType.RULE))
    if guidance:
        text = context_text(guidance, configured)
        argv += ["--rules", text]
    application = ArtifactApplication(deferred=deferred)
    validate_ancestor_names(context, application)
    validate_native_argv(tuple(argv))
    return NativeSessionArtifacts(application, tuple(argv))
