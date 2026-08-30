"""Closed resolved-spec facts shared by inspection surfaces.

The template domains own resolution and merge policy. This module owns only
the finite, presentation-free projection of one layered result: effective JSON
values plus the ordered declaration sources that contributed to every
surviving path. It never resolves secrets or invokes domain behavior.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, TypeAdapter

from agentworks.declared_resource import DeclaredResource
from agentworks.machine_output import project_json_value
from agentworks.resources.inheritance import LayerSource, LayerSourceKind
from agentworks.value_provenance import longest_prefix_value

if TYPE_CHECKING:
    from agentworks.machine_output import JsonObject, JsonValue
    from agentworks.resources.access import ResourceIdentity
    from agentworks.resources.inheritance import LayeredResolution, ProvenancePath


type ResolvedValueRole = Literal["defaulted", "inherited", "declared", "overlaid"]
type UnresolvedSpecReason = Literal["missing-selection", "instance-spec-unavailable"]

_JSON_ADAPTER: TypeAdapter[Any] = TypeAdapter(Any)


@dataclass(frozen=True, slots=True)
class ResolvedValueSource:
    """One declaration layer that contributed to a surviving value."""

    role: ResolvedValueRole
    resource_kind: str
    resource_name: str


@dataclass(frozen=True, slots=True)
class ResolvedPathProvenance:
    """The ordered sources for one unambiguous JSON value path."""

    path: tuple[str | int, ...]
    sources: tuple[ResolvedValueSource, ...]


@dataclass(frozen=True, slots=True)
class ResolvedSpec:
    """One fully resolved spec and complete surviving-value provenance."""

    spec: JsonObject
    provenance: tuple[ResolvedPathProvenance, ...]
    status: Literal["resolved"] = field(default="resolved", init=False)


@dataclass(frozen=True, slots=True)
class UnresolvedSpec:
    """A selected declaration that inspection cannot currently resolve."""

    selection: ResourceIdentity
    reason: UnresolvedSpecReason
    status: Literal["unresolved"] = field(default="unresolved", init=False)


type SpecResolution = ResolvedSpec | UnresolvedSpec


def resolved_spec_default_paths(model_type: type[object]) -> tuple[ProvenancePath, ...]:
    """Return every projected top-level field that a resolver must seed.

    Resolved domain dataclasses carry only one framework field, ``name``.
    ``AdminConfig`` is also its domain's resolved model, so its inherited
    declaration-envelope fields are excluded along with ``name``. Keeping this
    shape derivation beside projection prevents a new resolved field from
    silently losing default provenance.
    """
    return tuple((name,) for name in _resolved_field_names(model_type))


def project_resolved_spec[T](
    layered: LayeredResolution[T],
    selection: ResourceIdentity,
) -> ResolvedSpec:
    """Project one typed layered resolution through the closed JSON boundary."""
    dumped = _JSON_ADAPTER.dump_python(layered.value, mode="json")
    json_dumped = project_json_value(dumped)
    if not isinstance(json_dumped, dict):
        raise AssertionError("a resolved spec must project to a JSON object")

    field_names = _resolved_field_names(type(layered.value))
    spec: JsonObject = {name: json_dumped[name] for name in field_names}
    provenance = tuple(
        ResolvedPathProvenance(
            path=path,
            sources=tuple(_project_source(source, selection) for source in _sources_for_path(layered, path)),
        )
        for path in _provenance_paths(spec, frozenset(layered.provenance))
    )
    return ResolvedSpec(spec=spec, provenance=provenance)


def resolved_spec_data(resolution: SpecResolution) -> JsonObject:
    """Project one resolved or unresolved fact into its tagged JSON shape."""
    if isinstance(resolution, UnresolvedSpec):
        return {
            "status": resolution.status,
            "selection": {
                "kind": resolution.selection.kind,
                "name": resolution.selection.name,
            },
            "reason": resolution.reason,
        }
    return {
        "status": resolution.status,
        "spec": resolution.spec,
        "provenance": [
            {
                "path": list(item.path),
                "sources": [
                    {
                        "role": source.role,
                        "resource_kind": source.resource_kind,
                        "resource_name": source.resource_name,
                    }
                    for source in item.sources
                ],
            }
            for item in resolution.provenance
        ],
    }


def _resolved_field_names(model_type: type[object]) -> tuple[str, ...]:
    if issubclass(model_type, BaseModel):
        excluded = frozenset(DeclaredResource.model_fields) if issubclass(model_type, DeclaredResource) else frozenset()
        return tuple(name for name in model_type.model_fields if name not in excluded)
    if dataclasses.is_dataclass(model_type):
        return tuple(item.name for item in dataclasses.fields(model_type) if item.name != "name")
    raise AssertionError(f"unsupported resolved spec model: {model_type.__name__}")


def _sources_for_path[T](layered: LayeredResolution[T], path: ProvenancePath) -> tuple[LayerSource, ...]:
    sources = longest_prefix_value(layered.provenance, path)
    if not sources:
        rendered_path = "/".join(str(segment) for segment in path)
        raise AssertionError(f"resolved spec path lacks provenance: {rendered_path}")
    return sources


def _project_source(source: LayerSource, selection: ResourceIdentity) -> ResolvedValueSource:
    if source.kind is LayerSourceKind.DEFAULT:
        role: ResolvedValueRole = "defaulted"
    elif source.kind is LayerSourceKind.INSTANCE:
        role = "overlaid"
    elif source.resource_kind == selection.kind and source.name == selection.name:
        role = "declared"
    else:
        role = "inherited"
    return ResolvedValueSource(role, source.resource_kind, source.name)


def _provenance_paths(
    value: JsonValue,
    recorded_paths: frozenset[ProvenancePath],
    path: ProvenancePath = (),
    *,
    include_self: bool = True,
) -> tuple[ProvenancePath, ...]:
    """Return the finest truthful paths needed to inspect ``value``.

    A non-empty object can combine children from several layers even when
    its longest recorded prefix is only a default. Such a prefix does not
    truthfully describe the composite container, so the container is
    emitted only when one exact record owns its whole surviving subtree.
    Scalar leaves, empty containers, and final list positions are always
    material and remain independently inspectable.
    """
    paths: list[tuple[str | int, ...]] = []
    is_container = isinstance(value, dict | list)
    has_recorded_descendant = any(candidate != path and candidate[: len(path)] == path for candidate in recorded_paths)
    owns_subtree = path in recorded_paths and not has_recorded_descendant

    if path and include_self and (not is_container or not value or owns_subtree):
        paths.append(path)
    if isinstance(value, dict):
        for name, child in value.items():
            paths.extend(_provenance_paths(child, recorded_paths, (*path, name)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            item_path = (*path, index)
            paths.append(item_path)
            if isinstance(child, dict | list):
                paths.extend(_provenance_paths(child, recorded_paths, item_path, include_self=False))
    return tuple(paths)


__all__ = [
    "ResolvedPathProvenance",
    "ResolvedSpec",
    "ResolvedValueSource",
    "SpecResolution",
    "UnresolvedSpec",
    "project_resolved_spec",
    "resolved_spec_data",
    "resolved_spec_default_paths",
]
