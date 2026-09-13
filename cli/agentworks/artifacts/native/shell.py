"""Discoverable filesystem publication grouped by original owning scope."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from agentworks.artifacts.application import ArtifactApplication, ArtifactFile
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import artifact_file, json_text, skill_files

if TYPE_CHECKING:
    from agentworks.artifacts.model import ArtifactInputs


def shell_artifacts(inputs: ArtifactInputs, root: str, *, session: bool = False) -> ArtifactApplication:
    files: list[ArtifactFile] = []
    groups = []
    for group in inputs.groups():
        if not group:
            continue
        maps: dict[str, dict[str, object]] = {kind.value + "s": {} for kind in ArtifactType}
        group_root = f"{root}/scopes/{group.owner.component}/{group.owner.resource_name}"
        for item in group.items():
            content = item.content
            directory = f"{group_root}/{content.type.value}s"
            if content.type is ArtifactType.SKILL:
                publication = skill_files(directory, item)
            else:
                body = content.text
                if content.type is ArtifactType.AGENT:
                    body = json_text(
                        {
                            "name": content.name,
                            "description": content.description,
                            "instructions": content.text,
                            "native_options": content.native_options,
                        }
                    )
                suffix = "json" if content.type is ArtifactType.AGENT else "md"
                publication = (artifact_file(f"{directory}/{content.name}.{suffix}", body, (item,)),)
            files.extend(publication)
            maps[content.type.value + "s"][content.name] = {
                "files": [file.path.removeprefix(root + "/") for file in publication],
                "origin": asdict(item.origin),
                "source": item.provenance.source,
                "commit": item.provenance.commit,
            }
        groups.append({"owner": asdict(group.owner), **maps})
    if inputs:
        files.append(artifact_file(f"{root}/index.json", json_text({"groups": groups}), tuple(inputs.items())))
    return ArtifactApplication(tuple(files), artifacts_dir=root if session and inputs else None)
