"""Closed facts and renderers for ``agw resource show``.

The service projects one finalized registry row with its direct graph facts,
lazy read-only live usage, and attributable health checks. It does not traverse
beyond those direct edges, resolve secret values, or dispatch resource
mutations. Declarable rows additionally expose the normalized manifest envelope
represented by their loaded Pydantic model.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, assert_never

import yaml

from agentworks import output
from agentworks.declared_resource import METADATA_FIELDS, DeclaredResource, EnvelopeMetadata
from agentworks.doctor import HealthCheck, checks_for_resource, health_check_data
from agentworks.machine_output import JsonObject, JsonValue, project_origin
from agentworks.manifests.envelope import API_VERSION
from agentworks.resources.access import ResourceIdentity, resolve_resource
from agentworks.resources.graph import Enablement
from agentworks.resources.graph_query import (
    DatabaseLiveSource,
    FocusedGraphFacts,
    GraphEdge,
    focused_graph_facts,
    graph_edge_data,
    instance_ref_data,
)
from agentworks.resources.inspect import ResourceSummary, summarize_resource
from agentworks.resources.render import format_origin_line
from agentworks.terminal import sanitize_terminal_output

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.resources.kind import InstanceRef
    from agentworks.resources.registry import Registry


_UNSAFE_LINE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


@dataclass(frozen=True, slots=True)
class ResourceReadiness:
    """The stored readiness verdict for one enabled registry row."""

    is_ready: bool
    is_available: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class FocusedRelationships:
    """The canonically ordered direct declared edges touching one resource."""

    dependencies: tuple[GraphEdge, ...]
    dependents: tuple[GraphEdge, ...]


@dataclass(frozen=True, slots=True)
class ResourceShow:
    """The complete presentation-free facts for one registry row."""

    summary: ResourceSummary
    category: Literal["declarable", "capability"]
    enablement: Enablement
    readiness: ResourceReadiness | None
    relationships: FocusedRelationships
    used_by: tuple[InstanceRef, ...] | None
    diagnostics: tuple[HealthCheck, ...]
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


def show_resource(
    config: Config,
    registry: Registry,
    identity: ResourceIdentity,
    live_source: DatabaseLiveSource,
) -> ResourceShow:
    """Compose all authoritative focused facts for one finalized registry row."""
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

    enablement = registry.graph.enablement_of(identity.kind, identity.name)
    readiness: ResourceReadiness | None = None
    if enablement is Enablement.enabled:
        stored = registry.graph.readiness_of(identity.kind, identity.name)
        readiness = ResourceReadiness(
            is_ready=stored.is_ready,
            is_available=stored.is_available,
            reason=stored.reason,
        )

    focused: FocusedGraphFacts = focused_graph_facts(registry, identity, live_source)
    summary = summarize_resource(registry, identity, focused.used_by)
    diagnostics = checks_for_resource(config, registry, identity)

    if (summary.kind, summary.name) != (focused.focus.kind, focused.focus.name):
        raise AssertionError("focused graph identity diverged from the resource summary")
    if summary.reference_count != len(focused.dependents):
        raise AssertionError("focused dependents diverged from the resource summary")
    if summary.used_by_count != (None if focused.used_by is None else len(focused.used_by)):
        raise AssertionError("focused live usage diverged from the resource summary")
    if summary.disabled != (enablement is Enablement.disabled):
        raise AssertionError("resource enablement diverged from the resource summary")
    if summary.not_ready_reason != (None if readiness is None else readiness.reason):
        raise AssertionError("resource readiness diverged from the resource summary")

    return ResourceShow(
        summary=summary,
        category=handler.category,
        enablement=enablement,
        readiness=readiness,
        relationships=FocusedRelationships(focused.dependencies, focused.dependents),
        used_by=focused.used_by,
        diagnostics=diagnostics,
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
            "kind": shown.summary.kind,
            "name": shown.summary.name,
            "origin": project_origin(shown.summary.origin),
            "reference_count": shown.summary.reference_count,
            "used_by_count": shown.summary.used_by_count,
            "description": shown.summary.description,
            "not_ready_reason": shown.summary.not_ready_reason,
            "disabled": shown.summary.disabled,
            "category": shown.category,
            "enablement": shown.enablement.value,
            "readiness": readiness,
            "relationships": {
                "dependencies": [graph_edge_data(edge) for edge in shown.relationships.dependencies],
                "dependents": [graph_edge_data(edge) for edge in shown.relationships.dependents],
            },
            "used_by": None if shown.used_by is None else [instance_ref_data(ref) for ref in shown.used_by],
            "diagnostics": [health_check_data(check) for check in shown.diagnostics],
            "declaration": shown.declaration,
        }
    }


def _line_safe(value: str) -> str:
    sanitized = sanitize_terminal_output(value)
    return "".join(
        character for character in sanitized if unicodedata.category(character) not in _UNSAFE_LINE_CATEGORIES
    )


def _human_scalar(value: str | bool | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return _line_safe(value)


def _render_edge(edge: GraphEdge) -> None:
    source = f"{edge.source.kind}/{edge.source.name}"
    target = f"{edge.target.kind}/{edge.target.name}"
    declared_by = "null" if edge.declared_by is None else f"{edge.declared_by.kind}/{edge.declared_by.name}"
    output.info(
        f"{_line_safe(source)} -> {_line_safe(target)} "
        f"[{_line_safe(edge.relationship.value)}; usage={_human_scalar(edge.usage)}; "
        f"declared_by={_line_safe(declared_by)}]"
    )


def _render_edges(label: str, edges: tuple[GraphEdge, ...]) -> None:
    output.info(f"{label}:")
    with output.section():
        if not edges:
            output.info("none")
        for edge in edges:
            _render_edge(edge)


def render_resource_show(shown: ResourceShow) -> None:
    """Render every fact without allowing row text to create sibling lines."""
    output.info(f"Resource: {_line_safe(shown.summary.kind)}/{_line_safe(shown.summary.name)}")
    output.info(f"Category: {_line_safe(shown.category)}")
    output.info(f"Description: {_line_safe(shown.summary.description)}")
    output.info(f"Origin: {_line_safe(format_origin_line(shown.summary.origin))}")
    output.info(f"Reference count: {shown.summary.reference_count}")
    used_by_count = None if shown.summary.used_by_count is None else str(shown.summary.used_by_count)
    output.info(f"Used-by count: {_human_scalar(used_by_count)}")
    output.info(f"Not-ready reason: {_human_scalar(shown.summary.not_ready_reason)}")
    output.info(f"Disabled: {_human_scalar(shown.summary.disabled)}")
    output.info(f"Enablement: {_line_safe(shown.enablement.value)}")
    if shown.readiness is None:
        output.info("Readiness: null")
    else:
        output.info("Readiness:")
        with output.section():
            output.info(f"is_ready: {_human_scalar(shown.readiness.is_ready)}")
            output.info(f"is_available: {_human_scalar(shown.readiness.is_available)}")
            output.info(f"reason: {_human_scalar(shown.readiness.reason)}")

    output.info("Relationships:")
    with output.section():
        _render_edges("Dependencies", shown.relationships.dependencies)
        _render_edges("Dependents", shown.relationships.dependents)

    output.info("Used by:")
    with output.section():
        if shown.used_by is None:
            output.info("null")
        elif not shown.used_by:
            output.info("none")
        else:
            for ref in shown.used_by:
                output.info(f"{_line_safe(ref.instance_kind)}/{_line_safe(ref.instance_name)}")

    output.info("Diagnostics:")
    with output.section():
        if not shown.diagnostics:
            output.info("none")
        for check in shown.diagnostics:
            output.info(f"{_line_safe(check.status.value)}: {_line_safe(check.name)}")
            with output.section():
                output.info(f"message: {_human_scalar(check.message)}")
                output.info(f"hint: {_human_scalar(check.hint)}")

    if shown.declaration is None:
        output.info("Declaration: null")
        return

    output.info("Declaration:")
    document = yaml.safe_dump(
        shown.declaration,
        allow_unicode=False,
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
    "FocusedRelationships",
    "project_declaration",
    "render_resource_show",
    "resource_show_data",
    "show_resource",
]
