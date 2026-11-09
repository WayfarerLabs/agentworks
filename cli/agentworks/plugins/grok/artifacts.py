"""Grok Build native discovery and explicitly enabled session carriers."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import yaml

from agentworks import output
from agentworks.artifacts.application import ArtifactApplication, ArtifactDeferral, ArtifactFile
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import (
    NativeSessionArtifacts,
    ancestor_diagnostics,
    artifact_file,
    has_artifacts,
    native_home,
    persona_options,
    reject_flags,
    select_inputs,
    select_session_artifacts,
    skill_files,
    validate_names,
)
from agentworks.artifacts.native.guidance import guidance_rules, guidance_skills
from agentworks.artifacts.native.shell import filesystem_artifacts
from agentworks.artifacts.session_paths import session_package_prefix

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
    unsupported = {
        item.identity: sorted(set(item.content.metadata) & {"paths", "disable-model-invocation", "user-invocable"})
        for item in inputs.items()
        if item.content.type is ArtifactType.SKILL
        and set(item.content.metadata) & {"paths", "disable-model-invocation", "user-invocable"}
    }
    if unsupported:
        deferred = (
            *deferred,
            *(
                ArtifactDeferral(
                    input_id=identity,
                    destination="session",
                    reason="Grok indexed skill access cannot preserve native activation controls: " + ", ".join(fields),
                )
                for identity, fields in unsupported.items()
            ),
        )
        inputs = select_inputs(inputs, {item.identity for item in inputs.items() if item.identity not in unsupported})
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
    private = filesystem_artifacts(inputs, context.directory, session=True, aggregate_hints=True)
    files = [replace(file, native_identity=None) for file in private.files]
    if inputs:
        name = session_package_prefix(context.session_uuid) + "entry"
        description = (
            f"Use only when AGENTWORKS_SESSION_UUID={context.session_uuid}. "
            "Read the current Agentworks session artifact index before work or resuming work."
        )
        body = (
            f"Use this skill only when AGENTWORKS_SESSION_UUID={context.session_uuid}.\n\n"
            "Read AGENTWORKS_ARTIFACTS_DIR from this workload's current environment, then open "
            "index.json in that directory. Read every listed rule and hints file. For skills, use "
            "the index's names and descriptions to choose a SKILL.md and its supporting files. "
            "Re-read the current index after a managed restart; do not reuse an earlier run's path.\n"
        )
        header = yaml.safe_dump({"name": name, "description": description}, sort_keys=False)
        package = f"{root}/skills/{name}"
        files.append(
            ArtifactFile(
                f"{package}/SKILL.md",
                f"---\n{header}---\n{body}".encode(),
                tuple(item.origin_identity for item in inputs.items()),
                native_identity=f"skill:{name}",
                package_root=package,
            )
        )
        output.warn(
            "Grok session-home-skills advertises one session entry in the shared user skill catalog. "
            "Applicability is best effort; skills are accessed through the private index, "
            "not individual native commands."
        )
    application = ArtifactApplication(tuple(files), deferred, artifacts_dir=private.artifacts_dir)
    return NativeSessionArtifacts(application)
