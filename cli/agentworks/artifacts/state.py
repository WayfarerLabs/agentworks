"""Core-owned captures in the existing instance-state store."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agentworks.artifacts.bundle import resolve_bundle
from agentworks.artifacts.capture import capture_artifacts
from agentworks.artifacts.codec import decode_inputs, encode_inputs
from agentworks.artifacts.model import ArtifactComponent, ArtifactGroup, ArtifactOrigin
from agentworks.db import AppliedStateKey, AppliedStateSlice, Database, VersionedPayload
from agentworks.errors import StateError
from agentworks.sources import SourceRefError

if TYPE_CHECKING:
    from agentworks.artifacts.bundle import ArtifactBundle
    from agentworks.artifacts.declarations import ArtifactsConfig
    from agentworks.db.instance_state import InstanceKind, JsonObject
    from agentworks.package_sources import PackageCapture
    from agentworks.resources.registry import Registry

_COMPONENTS: dict[InstanceKind, tuple[ArtifactComponent, ...]] = {
    "vm": ("vm", "admin"),
    "agent": ("agent",),
    "workspace": ("workspace",),
    "session": ("session",),
}


class UnsupportedArtifactCaptureVersionError(StateError):
    """This release cannot interpret the stored artifact capture domain payload."""


@dataclass(frozen=True)
class CapturedArtifacts:
    """An owning component's declaration fingerprint and immutable source snapshot."""

    declaration: str
    inputs: ArtifactGroup


def _validate_owner(capture: CapturedArtifacts, kind: InstanceKind, name: str, component: ArtifactComponent) -> None:
    if component not in _COMPONENTS[kind] or re.fullmatch(r"[0-9a-f]{64}", capture.declaration) is None:
        raise ValueError
    if (capture.inputs.owner.component, capture.inputs.owner.resource_kind, capture.inputs.owner.resource_name) != (
        component,
        kind,
        name,
    ):
        raise ValueError
    if any(
        (item.origin.component, item.origin.resource_kind, item.origin.resource_name) != (component, kind, name)
        for item in capture.inputs.items()
    ):
        raise ValueError


def declaration_digest(registry: Registry, config: ArtifactsConfig) -> str:
    """Fingerprint effective declarations without opening their sources."""
    payload = []
    for name in config.bundles:
        bundle = resolve_bundle(registry, name).value
        maps = {
            kind.map_name: {key: spec.model_dump(mode="json") for key, spec in entries.items()}
            for kind, entries in bundle.type_maps()
        }
        payload.append((name, maps))
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def capture_owner(
    registry: Registry,
    kind: InstanceKind,
    name: str,
    component: ArtifactComponent,
    config: ArtifactsConfig,
    *,
    operation: PackageCapture | None = None,
) -> CapturedArtifacts:
    """Acquire and validate persistence before buffered captures can cause native effects."""
    bundles = []
    for bundle_name in config.bundles:
        bundle: ArtifactBundle = resolve_bundle(registry, bundle_name).value
        bundles.append((bundle_name, bundle))
    try:
        inputs = capture_artifacts(bundles, ArtifactOrigin(component, kind, name), operation=operation)
        encode_inputs(inputs)
    except SourceRefError as error:
        raise StateError(str(error), entity_kind=kind, entity_name=name) from None
    return CapturedArtifacts(declaration_digest(registry, config), inputs)


def encode_captures(captures: dict[ArtifactComponent, CapturedArtifacts]) -> VersionedPayload:
    return VersionedPayload(
        2,
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
    if record.payload.payload_version != 2:
        raise UnsupportedArtifactCaptureVersionError(
            "artifact captures require a different Agentworks version",
            hint="Use an Agentworks version that supports this capture. Stored data and file ownership were retained.",
        )
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
            if not isinstance(declaration, str):
                raise ValueError
            inputs = decode_inputs(value["content"])
            snapshot = CapturedArtifacts(declaration, inputs)
            _validate_owner(snapshot, record.instance_kind, record.instance_name, component)
            result[component] = snapshot
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
    """Validate direct writer inputs before replacing any previously readable capture."""
    try:
        _validate_owner(capture, kind, name, component)
    except ValueError:
        raise StateError(
            "artifact capture does not match its owner declaration", entity_kind=kind, entity_name=name
        ) from None
    captures = read_captures(db, kind, name)
    captures[component] = capture
    try:
        payload = encode_captures(captures)
    except SourceRefError:
        raise StateError("artifact capture cannot be persisted", entity_kind=kind, entity_name=name) from None
    db.instance_state.replace_applied_slices(kind, name, operation, {AppliedStateKey.ARTIFACT_INPUTS: payload})


def canonicalize_captures(record: AppliedStateSlice) -> VersionedPayload:
    if record.payload.payload_version != 2:
        return record.payload
    return encode_captures(decode_captures(record))
