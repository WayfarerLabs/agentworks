"""Immutable source-independent inputs shared by core and harness integrations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from _hashlib import HASH

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
class ArtifactInput:
    content: ArtifactContent
    provenance: ArtifactProvenance
    origin: ArtifactOrigin

    @property
    def identity(self) -> str:
        address = [
            self.origin.component,
            self.origin.resource_kind,
            self.origin.resource_name,
            self.origin.producer,
            self.origin.bundle,
            self.origin.entry,
            self.content.digest,
        ]
        return hashlib.sha256(json.dumps(address, separators=(",", ":")).encode()).hexdigest()


def _digest_part(digest: HASH, data: bytes) -> None:
    # Length framing prevents boundary ambiguity between adjacent member fields.
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)
