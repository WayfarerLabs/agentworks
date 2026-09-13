"""Core-owned captures in the existing instance-state store."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agentworks.artifacts.capture import capture_artifacts
from agentworks.artifacts.codec import decode_inputs, encode_inputs
from agentworks.artifacts.model import ArtifactComponent, ArtifactInput, ArtifactOrigin
from agentworks.db import AppliedStateKey, AppliedStateSlice, Database, VersionedPayload
from agentworks.errors import StateError
from agentworks.sources import SourceRefError

if TYPE_CHECKING:
    from agentworks.artifacts.bundle import ArtifactBundle
    from agentworks.artifacts.declarations import ArtifactsConfig
    from agentworks.db.instance_state import InstanceKind, JsonObject
    from agentworks.resources.registry import Registry

_COMPONENTS: dict[InstanceKind, tuple[ArtifactComponent, ...]] = {
    "vm": ("vm", "admin"),
    "agent": ("agent",),
    "workspace": ("workspace",),
    "session": ("session",),
}


@dataclass(frozen=True)
class CapturedArtifacts:
    """An owning component's declaration fingerprint and immutable source snapshot."""

    declaration: str
    inputs: tuple[ArtifactInput, ...]


def declaration_digest(registry: Registry, config: ArtifactsConfig) -> str:
    """Fingerprint effective declarations without opening their sources."""
    selected = [(name, registry.lookup("artifact-bundle", name).artifacts) for name in config.bundles]
    payload = [
        (name, {entry: spec.model_dump(mode="json") for entry, spec in entries.items()}) for name, entries in selected
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def capture_owner(
    registry: Registry, kind: InstanceKind, name: str, component: ArtifactComponent, config: ArtifactsConfig
) -> CapturedArtifacts:
    """Acquire the current local declaration only at its owning lifecycle operation."""
    bundles = []
    for bundle_name in config.bundles:
        bundle: ArtifactBundle = registry.lookup("artifact-bundle", bundle_name)
        bundles.append((bundle_name, bundle.artifacts))
    try:
        inputs = capture_artifacts(bundles, ArtifactOrigin(component, kind, name))
    except SourceRefError as error:
        raise StateError(str(error), entity_kind=kind, entity_name=name) from None
    return CapturedArtifacts(declaration_digest(registry, config), inputs)


def encode_captures(captures: dict[ArtifactComponent, CapturedArtifacts]) -> VersionedPayload:
    return VersionedPayload(
        1,
        cast(
            "JsonObject",
            {
                "components": {
                    component: {"declaration": snapshot.declaration, "content": encode_inputs(snapshot.inputs)}
                    for component, snapshot in captures.items()
                }
            },
        ),
    )


def decode_captures(record: AppliedStateSlice) -> dict[ArtifactComponent, CapturedArtifacts]:
    """Validate persisted input content and its actual owner without echoing values."""
    if record.key is not AppliedStateKey.ARTIFACT_INPUTS:
        raise TypeError("artifact capture requires its matching applied slice")
    if record.payload.payload_version != 1:
        raise StateError("artifact captures require a different Agentworks version")
    try:
        payload = record.payload.value
        components = payload["components"]
        if set(payload) != {"components"} or not isinstance(components, dict):
            raise ValueError
        result: dict[ArtifactComponent, CapturedArtifacts] = {}
        for component, value in components.items():
            if component not in _COMPONENTS[record.instance_kind] or not isinstance(value, dict):
                raise ValueError
            if set(value) != {"declaration", "content"}:
                raise ValueError
            declaration = value["declaration"]
            if not isinstance(declaration, str) or re.fullmatch(r"[0-9a-f]{64}", declaration) is None:
                raise ValueError
            inputs = decode_inputs(value["content"])
            if any(
                (item.origin.component, item.origin.resource_kind, item.origin.resource_name)
                != (component, record.instance_kind, record.instance_name)
                for item in inputs
            ):
                raise ValueError
            result[component] = CapturedArtifacts(declaration, inputs)
        return result
    except (KeyError, TypeError, ValueError, SourceRefError, RecursionError):
        raise StateError("stored artifact capture is malformed") from None


def read_captures(db: Database, kind: InstanceKind, name: str) -> dict[ArtifactComponent, CapturedArtifacts]:
    for record in db.instance_state.get_applied_slices(kind, name):
        if record.key is AppliedStateKey.ARTIFACT_INPUTS:
            return decode_captures(record)
    return {}


def write_capture(
    db: Database,
    kind: InstanceKind,
    name: str,
    component: ArtifactComponent,
    capture: CapturedArtifacts,
    *,
    operation: str,
) -> None:
    if component not in _COMPONENTS[kind]:
        raise StateError("artifact capture component does not belong to its owner")
    captures = read_captures(db, kind, name)
    captures[component] = capture
    db.instance_state.replace_applied_slices(
        kind, name, operation, {AppliedStateKey.ARTIFACT_INPUTS: encode_captures(captures)}
    )


def canonicalize_captures(record: AppliedStateSlice) -> VersionedPayload:
    if record.payload.payload_version != 1:
        return record.payload
    return encode_captures(decode_captures(record))
