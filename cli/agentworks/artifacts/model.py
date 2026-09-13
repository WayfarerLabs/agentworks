"""Immutable source-independent inputs shared by core and harness integrations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from _hashlib import HASH
    from collections.abc import Iterator, Mapping

ArtifactComponent = Literal["vm", "admin", "agent", "workspace", "session"]
ArtifactFacet = Literal["vm", "user", "workspace", "session"]
ALLOWED_DEFERRALS: dict[ArtifactFacet, tuple[ArtifactFacet, ...]] = {
    "vm": ("user", "workspace", "session"),
    "user": ("session",),
    "workspace": ("session",),
    "session": (),
}


class ArtifactType(StrEnum):
    HINT = "hint"
    RULE = "rule"
    SKILL = "skill"
    AGENT = "agent"


@dataclass(frozen=True)
class ArtifactOwner:
    """The actual owning scope, independent of any receiving facet or producer."""

    component: ArtifactComponent
    resource_kind: str
    resource_name: str


@dataclass(frozen=True)
class ArtifactMember:
    """A complete package member; text records the normalization decision."""

    path: str
    data: bytes
    executable: bool = False
    text: bool = False


@dataclass(frozen=True)
class ArtifactContent:
    """Canonical content with immutable JSON metadata and native persona options.

    JSON strings keep nested declaration objects out of the integration boundary.
    Readers may decode their own mutable copy without modifying another consumer.
    """

    type: ArtifactType
    name: str
    description: str = ""
    text: str = ""
    members: tuple[ArtifactMember, ...] = ()
    metadata_json: str = "{}"
    native_options_json: str = "{}"

    @property
    def metadata(self) -> dict[str, object]:
        return cast("dict[str, object]", json.loads(self.metadata_json))

    @property
    def native_options(self) -> dict[str, object]:
        return cast("dict[str, object]", json.loads(self.native_options_json))

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        header = [self.type.value, self.name, self.description, self.text, self.metadata_json, self.native_options_json]
        _digest_part(digest, json.dumps(header, ensure_ascii=True, separators=(",", ":")).encode())
        for member in sorted(self.members, key=lambda item: item.path):
            _digest_part(digest, member.path.encode("utf-8"))
            _digest_part(digest, bytes([member.executable]))
            _digest_part(digest, member.data)
        return digest.hexdigest()


@dataclass(frozen=True)
class ArtifactOrigin:
    """Identity of the owner consuming a bundle entry."""

    component: ArtifactComponent
    resource_kind: str
    resource_name: str
    producer: str = "core"
    bundle: str = ""
    entry: str = ""

    @property
    def owner(self) -> ArtifactOwner:
        return ArtifactOwner(self.component, self.resource_kind, self.resource_name)

    @property
    def identity(self) -> str:
        address = [self.component, self.resource_kind, self.resource_name, self.producer, self.bundle, self.entry]
        return hashlib.sha256(json.dumps(address, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ArtifactProvenance:
    """Credential-free acquisition evidence, separate from logical identity."""

    source: str = "inline"
    requested_ref: str = ""
    selected_path: str = ""
    commit: str = ""


@dataclass(frozen=True)
class ArtifactReplacement:
    """Compact evidence of a replaced definition; no retained artifact bodies."""

    origin: ArtifactOrigin
    provenance: ArtifactProvenance
    digest: str


@dataclass(frozen=True)
class ArtifactInput:
    content: ArtifactContent
    provenance: ArtifactProvenance
    origin: ArtifactOrigin

    replacements: tuple[ArtifactReplacement, ...] = ()

    @property
    def origin_identity(self) -> str:
        address = [
            self.origin.component,
            self.origin.resource_kind,
            self.origin.resource_name,
            self.content.type.value,
            self.content.name,
        ]
        return hashlib.sha256(json.dumps(address, separators=(",", ":")).encode()).hexdigest()

    @property
    def identity(self) -> str:
        return hashlib.sha256((self.origin_identity + self.content.digest).encode()).hexdigest()


@dataclass(frozen=True)
class ArtifactGroup:
    """Per-type maps validated at the plugin and persisted input boundaries."""

    owner: ArtifactOwner
    hints: Mapping[str, ArtifactInput] = field(default_factory=dict)
    rules: Mapping[str, ArtifactInput] = field(default_factory=dict)
    skills: Mapping[str, ArtifactInput] = field(default_factory=dict)
    agents: Mapping[str, ArtifactInput] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for artifact_type in ArtifactType:
            attribute = artifact_type.value + "s"
            values = dict(getattr(self, attribute))
            for name, item in values.items():
                if (
                    item.origin.owner != self.owner
                    or item.content.type != artifact_type
                    or item.content.name != name
                    or item.origin.entry != name
                ):
                    raise ValueError("artifact map entry does not match its owner, type or name")
            object.__setattr__(self, attribute, MappingProxyType(values))

    def items(self) -> Iterator[ArtifactInput]:
        for values in (self.hints, self.rules, self.skills, self.agents):
            yield from values.values()

    def select(self, identities: set[str]) -> ArtifactGroup:
        return ArtifactGroup(
            self.owner,
            **{
                kind.value + "s": {
                    name: item for name, item in getattr(self, kind.value + "s").items() if item.identity in identities
                }
                for kind in ArtifactType
            },
        )

    def __bool__(self) -> bool:
        return bool(self.hints or self.rules or self.skills or self.agents)


@dataclass(frozen=True)
class ArtifactInputs:
    """A receiving facet's local group and separate original-owner deferrals."""

    local: ArtifactGroup | None = None
    deferred: Mapping[ArtifactOwner, ArtifactGroup] = field(default_factory=dict)

    def __post_init__(self) -> None:
        deferred = dict(self.deferred)
        if any(owner != group.owner for owner, group in deferred.items()):
            raise ValueError("deferred artifact group does not match its original owner")
        if self.local is not None and self.local.owner in deferred:
            raise ValueError("local artifact owner cannot also be a deferred owner")
        object.__setattr__(self, "deferred", MappingProxyType(deferred))

    def groups(self) -> tuple[ArtifactGroup, ...]:
        return (*self.deferred.values(), *((self.local,) if self.local is not None else ()))

    def items(self) -> Iterator[ArtifactInput]:
        for group in self.groups():
            yield from group.items()

    def __bool__(self) -> bool:
        return any(self.groups())


def _digest_part(digest: HASH, data: bytes) -> None:
    # Length framing prevents boundary ambiguity between adjacent member fields.
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)
