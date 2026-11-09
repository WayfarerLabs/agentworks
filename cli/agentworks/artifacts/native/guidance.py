"""Project canonical guidance into separately owned native files and skills."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import yaml

from agentworks.artifacts.application import ArtifactFile
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.session_paths import session_package_prefix

if TYPE_CHECKING:
    from agentworks.artifacts.application import SessionArtifactContext
    from agentworks.artifacts.model import ArtifactInput, ArtifactInputs


def _owner(item: ArtifactInput) -> str:
    return f"{item.origin.component}/{item.origin.resource_name}"


def guidance_entries(inputs: ArtifactInputs) -> tuple[tuple[ArtifactInput, ...], ...]:
    """Rules remain individual; only hints of the same original owner aggregate."""
    return tuple(
        entry
        for group in inputs.groups()
        for entry in (
            *((tuple(group.hints.values()),) if group.hints else ()),
            *((item,) for item in group.rules.values()),
        )
    )


def _guidance_name(items: tuple[ArtifactInput, ...]) -> str:
    first = items[0]
    label = "hints" if first.content.type is ArtifactType.HINT else f"rule-{first.content.name}"
    owner = [first.origin.component, first.origin.resource_kind, first.origin.resource_name]
    identity = [*owner, first.content.type.value]
    if first.content.type is ArtifactType.RULE:
        identity.append(first.content.name)
    suffix = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:10]
    return f"agentworks-{label[:42].rstrip('-')}-{suffix}"


def _body(items: tuple[ArtifactInput, ...]) -> str:
    if items[0].content.type is ArtifactType.RULE:
        return items[0].content.text
    return "\n\n".join(f"## {item.content.name}\n\n{item.content.text}" for item in items)


def _session_name(items: tuple[ArtifactInput, ...], context: SessionArtifactContext) -> str:
    identity = "\n".join(item.origin_identity for item in items)
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:10]
    return session_package_prefix(context.session_uuid, context.run_id) + suffix


def _condition(context: SessionArtifactContext) -> str:
    return f"Use only when AGENTWORKS_SESSION_UUID={context.session_uuid} and AGENTWORKS_RUN_ID={context.run_id}. "


def guidance_skills(
    root: str, inputs: ArtifactInputs, *, context: SessionArtifactContext | None = None, namespace: str = ""
) -> tuple[ArtifactFile, ...]:
    files = []
    for items in guidance_entries(inputs):
        first = items[0]
        name = _guidance_name(items) if context is None else _session_name(items, context)
        label = "setup hints" if first.content.type is ArtifactType.HINT else f"rule {first.content.name}"
        description = f"Read before working: {label} from {_owner(first)}. {first.content.description}".strip()
        body = _body(items)
        if context is not None:
            condition = _condition(context)
            description = condition + description
            body = condition + "Otherwise do not apply this guidance.\n\n" + body
        metadata = yaml.safe_dump({"name": name, "description": description[:1024]}, sort_keys=False)
        package = f"{root}/{name}"
        files.append(
            ArtifactFile(
                f"{package}/SKILL.md",
                f"---\n{metadata}---\n{body}".encode(),
                tuple(item.origin_identity for item in items),
                native_identity=f"skill:{namespace}{name}",
                package_root=package,
            )
        )
    return tuple(files)


def guidance_rules(root: str, inputs: ArtifactInputs) -> tuple[ArtifactFile, ...]:
    return tuple(
        ArtifactFile(
            f"{root}/{_guidance_name(items)}.md",
            _body(items).encode(),
            tuple(item.origin_identity for item in items),
        )
        for items in guidance_entries(inputs)
    )


def session_skill_files(root: str, item: ArtifactInput, context: SessionArtifactContext) -> tuple[ArtifactFile, ...]:
    """Qualify the native entrypoint while retaining every supporting package member."""
    name = _session_name((item,), context)
    condition = _condition(context)
    metadata = {
        **item.content.metadata,
        "name": name,
        "description": (condition + f"Skill {item.content.name} from {_owner(item)}: " + item.content.description)[
            :1024
        ],
    }
    header = yaml.safe_dump(metadata, sort_keys=False)
    body = condition + "Otherwise do not use this skill.\n\n" + item.content.text
    entrypoint = f"---\n{header}---\n{body}".encode()
    package = f"{root}/{name}"
    return tuple(
        ArtifactFile(
            f"{package}/{member.path}",
            entrypoint if member.path == "SKILL.md" else member.data,
            (item.origin_identity,),
            executable=member.executable,
            native_identity=f"skill:{name}",
            package_root=package,
        )
        for member in item.content.members
    )
