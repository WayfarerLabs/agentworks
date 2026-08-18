"""Closed facts and renderers for ``agw resource show``.

The service projects one finalized registry row without traversing graph
relationships, opening the state database, resolving secrets, or dispatching
resource operations. Declarable rows additionally expose the normalized
manifest envelope represented by their loaded Pydantic model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, assert_never

import yaml

from agentworks import output
from agentworks.declared_resource import METADATA_FIELDS, DeclaredResource, EnvelopeMetadata
from agentworks.machine_output import JsonObject, JsonValue, project_origin
from agentworks.manifests.envelope import API_VERSION
from agentworks.resources.access import ResourceIdentity, resolve_resource
from agentworks.resources.graph import Enablement
from agentworks.resources.render import format_origin_line
from agentworks.terminal import sanitize_terminal_output

if TYPE_CHECKING:
    from agentworks.origin import Origin
    from agentworks.resources.registry import Registry


@dataclass(frozen=True, slots=True)
class ResourceReadiness:
    """The stored readiness verdict for one enabled registry row."""

    is_ready: bool
    is_available: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class ResourceShow:
    """The complete presentation-free facts for one registry row."""

    identity: ResourceIdentity
    category: Literal["declarable", "capability"]
    description: str
    origin: Origin | None
    enablement: Enablement
    readiness: ResourceReadiness | None
    declaration: JsonObject | None


def _json_value(value: Any) -> JsonValue:
    """Validate Pydantic JSON-mode output against the closed JSON v1 carrier."""
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AssertionError("declaration JSON values must be finite")
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise AssertionError("declaration JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    raise AssertionError(f"Pydantic JSON mode produced a non-JSON value: {type(value).__name__}")


def project_declaration(kind: str, resource: DeclaredResource) -> JsonObject:
    """Reconstruct one normalized manifest from a loaded declarable row."""
    framework_fields = set(DeclaredResource.model_fields) - METADATA_FIELDS
    declared_fields = set(type(resource).model_fields) - framework_fields
    dumped = resource.model_dump(mode="json", include=declared_fields, exclude_none=True)
    json_dumped = _json_value(dumped)
    if not isinstance(json_dumped, dict):
        raise AssertionError("a declared resource must project to a JSON object")

    metadata: JsonObject = {name: json_dumped[name] for name in EnvelopeMetadata.model_fields if name in json_dumped}
    spec: JsonObject = {
        name: json_dumped[name]
        for name in type(resource).model_fields
        if name not in METADATA_FIELDS and name not in framework_fields and name in json_dumped
    }
    return {
        "apiVersion": API_VERSION,
        "kind": kind,
        "metadata": metadata,
        "spec": spec,
    }


def show_resource(registry: Registry, identity: ResourceIdentity) -> ResourceShow:
    """Return uniform facts for one row in a finalized request registry."""
    from agentworks.resources import KIND_REGISTRY

    resolved = resolve_resource(registry, identity)
    handler = KIND_REGISTRY[identity.kind]
    resource = resolved.resource

    if handler.category == "declarable":
        if not isinstance(resource, DeclaredResource):
            raise AssertionError(f"declarable kind {identity.kind!r} published a non-declared row")
        declaration = project_declaration(identity.kind, resource)
    elif handler.category == "capability":
        if isinstance(resource, DeclaredResource):
            raise AssertionError(f"capability kind {identity.kind!r} published a declared row")
        declaration = None
    else:
        assert_never(handler.category)

    raw_description = getattr(resource, "description", None)
    if raw_description is not None and not isinstance(raw_description, str):
        raise AssertionError(f"resource description must be a string or null, got {type(raw_description).__name__}")

    enablement = registry.graph.enablement_of(identity.kind, identity.name)
    readiness: ResourceReadiness | None = None
    if enablement is Enablement.enabled:
        stored = registry.graph.readiness_of(identity.kind, identity.name)
        readiness = ResourceReadiness(
            is_ready=stored.is_ready,
            is_available=stored.is_available,
            reason=stored.reason,
        )

    return ResourceShow(
        identity=identity,
        category=handler.category,
        description=raw_description or "",
        origin=resolved.origin,
        enablement=enablement,
        readiness=readiness,
        declaration=declaration,
    )


def resource_show_data(shown: ResourceShow) -> JsonObject:
    """Project resource facts into the closed ``resource.show`` JSON shape."""
    readiness: JsonObject | None = None
    if shown.readiness is not None:
        readiness = {
            "is_ready": shown.readiness.is_ready,
            "is_available": shown.readiness.is_available,
            "reason": shown.readiness.reason,
        }
    return {
        "resource": {
            "kind": shown.identity.kind,
            "name": shown.identity.name,
            "category": shown.category,
            "description": shown.description,
            "origin": project_origin(shown.origin),
            "enablement": shown.enablement.value,
            "readiness": readiness,
            "declaration": shown.declaration,
        }
    }


def _line_safe(value: str) -> str:
    return sanitize_terminal_output(value).replace("\n", "").replace("\t", "")


def _human_scalar(value: str | bool | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return _line_safe(value)


def render_resource_show(shown: ResourceShow) -> None:
    """Render every fact without allowing row text to create sibling lines."""
    output.info(f"Resource: {_line_safe(shown.identity.kind)}/{_line_safe(shown.identity.name)}")
    output.info(f"Category: {_line_safe(shown.category)}")
    output.info(f"Description: {_line_safe(shown.description)}")
    output.info(f"Origin: {_line_safe(format_origin_line(shown.origin))}")
    output.info(f"Enablement: {_line_safe(shown.enablement.value)}")
    if shown.readiness is None:
        output.info("Readiness: null")
    else:
        output.info("Readiness:")
        with output.section():
            output.info(f"is_ready: {_human_scalar(shown.readiness.is_ready)}")
            output.info(f"is_available: {_human_scalar(shown.readiness.is_available)}")
            output.info(f"reason: {_human_scalar(shown.readiness.reason)}")

    if shown.declaration is None:
        output.info("Declaration: null")
        return

    output.info("Declaration:")
    document = yaml.safe_dump(
        shown.declaration,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    safe_document = sanitize_terminal_output(document)
    with output.section():
        for line in safe_document.rstrip("\n").split("\n"):
            output.info(line)


__all__ = [
    "ResourceReadiness",
    "ResourceShow",
    "project_declaration",
    "render_resource_show",
    "resource_show_data",
    "show_resource",
]
