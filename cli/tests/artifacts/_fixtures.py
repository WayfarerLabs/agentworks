"""Small constructors for explicitly grouped artifact test inputs."""

from __future__ import annotations

from agentworks.artifacts.model import ArtifactGroup, ArtifactInput, ArtifactInputs, ArtifactOwner, ArtifactType


def group(*items: ArtifactInput, owner: ArtifactOwner | None = None) -> ArtifactGroup:
    scope = owner or (items[0].origin.owner if items else ArtifactOwner("session", "session", "s1"))
    maps: dict[str, dict[str, ArtifactInput]] = {kind.value + "s": {} for kind in ArtifactType}
    for item in items:
        entries = maps[item.content.type.value + "s"]
        if item.content.name in entries:
            raise ValueError("duplicate fixture artifact key")
        entries[item.content.name] = item
    return ArtifactGroup(scope, **maps)


def received(*items: ArtifactInput) -> ArtifactInputs:
    owners: dict[ArtifactOwner, list[ArtifactInput]] = {}
    for item in items:
        owners.setdefault(item.origin.owner, []).append(item)
    groups = [group(*values) for values in owners.values()]
    return ArtifactInputs(local=groups[-1] if groups else None, deferred={value.owner: value for value in groups[:-1]})
