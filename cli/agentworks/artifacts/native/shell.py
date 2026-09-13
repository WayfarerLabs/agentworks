"""Discoverable filesystem publication for a shell, with no model-context claim."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.artifacts.application import ArtifactApplication, ArtifactFile
from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.native.common import artifact_file, json_text, skill_files, validate_names

if TYPE_CHECKING:
    from agentworks.artifacts.model import ArtifactInput


def shell_artifacts(inputs: tuple[ArtifactInput, ...], root: str, *, session: bool = False) -> ArtifactApplication:
    validate_names(inputs)
    files: list[ArtifactFile] = []
    entries = []
    for item in inputs:
        content = item.content
        directory = f"{root}/{content.type.value}s"
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
            publication = (
                artifact_file(
                    f"{directory}/{content.name}.{suffix}",
                    body,
                    (item,),
                    identity=f"{content.type.value}:{content.name}",
                ),
            )
        files.extend(publication)
        entries.append(
            {
                "type": content.type.value,
                "name": content.name,
                "files": [file.path.removeprefix(root + "/") for file in publication],
                "origin": {
                    "component": item.origin.component,
                    "resource_kind": item.origin.resource_kind,
                    "resource_name": item.origin.resource_name,
                    "bundle": item.origin.bundle,
                    "entry": item.origin.entry,
                    "producer": item.origin.producer,
                },
                "source": item.provenance.source,
                "commit": item.provenance.commit,
            }
        )
    if inputs:
        files.append(artifact_file(f"{root}/index.json", json_text({"artifacts": entries}), inputs))
    return ArtifactApplication(tuple(files), artifacts_dir=root if session and inputs else None)
