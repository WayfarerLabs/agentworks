"""Claude Code's native artifact files and session-only carriers."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import yaml

from agentworks import output
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
    select_session_artifacts,
    skill_files,
    validate_ancestor_names,
    validate_names,
    validate_native_argv,
)
from agentworks.ssh import SSHError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agentworks.artifacts.application import SessionArtifactContext
    from agentworks.artifacts.model import ArtifactInput, ArtifactInputs
    from agentworks.transports import Transport


_OPTIONS = {"model": str, "tools": list, "disallowedTools": list, "maxTurns": int}


_SESSION_WORKAROUNDS = {
    ArtifactType.HINT: "session-prompt",
    ArtifactType.RULE: "session-prompt",
    ArtifactType.SKILL: "session-skill-plugin",
    ArtifactType.AGENT: "session-agent-definitions",
}


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
    has_skills = ArtifactType.SKILL in selected_types or any(name.startswith("skill:") for name in ancestor_names)
    has_agents = ArtifactType.AGENT in selected_types or any(name.startswith("agent:") for name in ancestor_names)
    forbidden = {"--bare", "--settings", "--setting-sources"}
    if selected_types & {ArtifactType.HINT, ArtifactType.RULE}:
        forbidden.update(
            {
                "--append-system-prompt",
                "--append-system-prompt-file",
                "--system-prompt",
                "--system-prompt-file",
                "--system-prompt-snapshot",
            }
        )
    if has_skills:
        forbidden.update({"--plugin-dir", "--disable-slash-commands"})
    if has_agents:
        forbidden.add("--agents")
    reject_flags(extra_args, forbidden, "claude-code")
    files = []
    argv: list[str] = []
    guidance = tuple(item for item in inputs.items() if item.content.type in (ArtifactType.HINT, ArtifactType.RULE))
    if guidance:
        path = f"{context.directory}/instructions.md"
        files.append(artifact_file(path, context_text(guidance, configured), guidance))
        argv += ["--append-system-prompt-file", path]
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
    application = ArtifactApplication(tuple(files), deferred)
    validate_ancestor_names(context, application)
    validate_native_argv(tuple(argv))
    return NativeSessionArtifacts(application, tuple(argv))


def session_prompt_snapshot(runner: Transport, environment: Mapping[str, str]) -> tuple[str, ...]:
    """Refresh appended guidance on Claude versions that otherwise snapshot it.

    Before 2.1.265, explicit system-prompt flags disabled recording automatically.
    https://code.claude.com/docs/en/cli-reference#system-prompt-flags-in-resumed-conversations
    """
    version = None
    try:
        result = runner.run(
            "\"$SHELL\" -lic 'claude --version'",
            env=dict(environment),
            tty=False,
            check=False,
            timeout=20,
        )
        if result.returncode == 0:
            version = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout)
    except SSHError:
        pass
    if version is None:
        output.warn(
            "Claude session-prompt workaround: unable to determine the installed version; "
            "continuing without --system-prompt-snapshot. Updated session rules and hints may stay stale on resume."
        )
        return ()
    return ("--system-prompt-snapshot", "off") if tuple(map(int, version.groups())) >= (2, 1, 265) else ()
