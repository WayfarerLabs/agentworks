"""Closed facts and renderers for ``agw resource show``.

The service projects one finalized registry row with its direct graph facts,
finalized live usage, and attributable health checks. It does not traverse
beyond those direct edges, resolve secret values, or dispatch resource
mutations. Declarable rows additionally expose the normalized manifest envelope
represented by their loaded Pydantic model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, assert_never

from agentworks import output
from agentworks.declared_resource import FRAMEWORK_FIELDS, METADATA_FIELDS, DeclaredResource, EnvelopeMetadata
from agentworks.doctor import HealthCheck, checks_for_resource, health_check_data
from agentworks.machine_output import JsonObject, project_json_value, project_origin
from agentworks.manifests.envelope import API_VERSION
from agentworks.resources.access import ResourceIdentity, resolve_resource
from agentworks.resources.graph import Enablement, Readiness
from agentworks.resources.graph_query import (
    FocusedGraphFacts,
    GraphEdge,
    focused_graph_facts,
    graph_edge_data,
    instance_ref_data,
)
from agentworks.resources.inspect import ResourceSummary, summarize_resource
from agentworks.resources.kind import ResolvedSpecKind
from agentworks.resources.render import format_origin_line, sanitize_fact_line, yaml_document_lines
from agentworks.resources.resolved_spec import ResolvedSpec, resolved_spec_data

if TYPE_CHECKING:
    from agentworks.capabilities.secret_backend import TtyInteractionAccess
    from agentworks.config import Config
    from agentworks.resources.kind import InstanceRef
    from agentworks.resources.registry import Registry


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
    readiness: Readiness | None
    relationships: FocusedRelationships
    used_by: tuple[InstanceRef, ...] | None
    diagnostics: tuple[HealthCheck, ...]
    declaration: JsonObject | None
    resolution: ResolvedSpec | None = None


def project_declaration(kind: str, resource: DeclaredResource) -> JsonObject:
    """Reconstruct one normalized manifest from a loaded declarable row."""
    declared_fields = set(type(resource).model_fields) - FRAMEWORK_FIELDS
    dumped = resource.model_dump(mode="json", include=declared_fields, exclude_none=True)
    json_dumped = project_json_value(dumped)
    if not isinstance(json_dumped, dict):
        raise AssertionError("a declared resource must project to a JSON object")

    metadata: JsonObject = {name: json_dumped[name] for name in EnvelopeMetadata.model_fields if name in json_dumped}
    spec: JsonObject = {
        name: json_dumped[name]
        for name in type(resource).model_fields
        if name not in METADATA_FIELDS and name not in FRAMEWORK_FIELDS and name in json_dumped
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
    *,
    tty_access: TtyInteractionAccess,
) -> ResourceShow:
    """Compose all authoritative focused facts for one finalized registry row."""
    from agentworks.resources import KIND_REGISTRY
    from agentworks.secrets.policy import require_exact_tty_interaction_access

    require_exact_tty_interaction_access(tty_access)

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
    readiness: Readiness | None = None
    if enablement is Enablement.enabled:
        readiness = registry.graph.readiness_of(identity.kind, identity.name)

    focused: FocusedGraphFacts = focused_graph_facts(registry, identity)
    summary = summarize_resource(registry, identity, focused.used_by)
    diagnostics = checks_for_resource(config, registry, identity, tty_access=tty_access)
    resolution = handler.resolve_for_show(registry, identity.name) if isinstance(handler, ResolvedSpecKind) else None

    return ResourceShow(
        summary=summary,
        category=handler.category,
        enablement=enablement,
        readiness=readiness,
        relationships=FocusedRelationships(focused.dependencies, focused.dependents),
        used_by=focused.used_by,
        diagnostics=diagnostics,
        declaration=declaration,
        resolution=resolution,
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
    resource: JsonObject = {
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
    if shown.resolution is not None:
        resource["resolution"] = resolved_spec_data(shown.resolution)
    return {"resource": resource}


def _human_scalar(value: str | bool | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return sanitize_fact_line(value)


def _render_edge(edge: GraphEdge) -> None:
    source = f"{edge.source.kind}/{edge.source.name}"
    target = f"{edge.target.kind}/{edge.target.name}"
    declared_by = "null" if edge.declared_by is None else f"{edge.declared_by.kind}/{edge.declared_by.name}"
    output.info(
        f"{sanitize_fact_line(source)} -> {sanitize_fact_line(target)} "
        f"[{sanitize_fact_line(edge.relationship.value)}; usage={_human_scalar(edge.usage)}; "
        f"declared_by={sanitize_fact_line(declared_by)}]"
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
    output.info(f"Resource: {sanitize_fact_line(shown.summary.kind)}/{sanitize_fact_line(shown.summary.name)}")
    output.info(f"Category: {sanitize_fact_line(shown.category)}")
    output.info(f"Description: {sanitize_fact_line(shown.summary.description)}")
    output.info(f"Origin: {sanitize_fact_line(format_origin_line(shown.summary.origin))}")
    output.info(f"Reference count: {shown.summary.reference_count}")
    used_by_count = None if shown.summary.used_by_count is None else str(shown.summary.used_by_count)
    output.info(f"Used-by count: {_human_scalar(used_by_count)}")
    output.info(f"Not-ready reason: {_human_scalar(shown.summary.not_ready_reason)}")
    output.info(f"Disabled: {_human_scalar(shown.summary.disabled)}")
    output.info(f"Enablement: {sanitize_fact_line(shown.enablement.value)}")
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
                output.info(f"{sanitize_fact_line(ref.instance_kind)}/{sanitize_fact_line(ref.instance_name)}")

    output.info("Diagnostics:")
    with output.section():
        if not shown.diagnostics:
            output.info("none")
        for check in shown.diagnostics:
            output.info(f"{sanitize_fact_line(check.status.value)}: {sanitize_fact_line(check.name)}")
            with output.section():
                output.info(f"message: {_human_scalar(check.message)}")
                output.info(f"hint: {_human_scalar(check.hint)}")

    if shown.resolution is not None:
        output.info("Resolved spec:")
        with output.section():
            for line in yaml_document_lines(shown.resolution.spec):
                output.info(line)
        output.info("Resolved spec provenance:")
        with output.section():
            for item in shown.resolution.provenance:
                path = json.dumps(item.path, ensure_ascii=True, separators=(",", ":"))
                sources = ", ".join(
                    f"{source.role}:{source.resource_kind}/{source.resource_name}" for source in item.sources
                )
                output.info(f"{sanitize_fact_line(path)}: {sanitize_fact_line(sources)}")

    if shown.declaration is None:
        output.info("Declaration: null")
        return

    output.info("Declaration:")
    with output.section():
        for line in yaml_document_lines(shown.declaration):
            output.info(line)


__all__ = [
    "ResourceShow",
    "FocusedRelationships",
    "project_declaration",
    "render_resource_show",
    "resource_show_data",
    "show_resource",
]
