"""Effective integration activation merging, validation, and dependency projection."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from agentworks.capabilities.config import capability_config_model, validate_capability_config
from agentworks.resources.reference import ResourceReference, sourced_references
from agentworks.schema import CapabilityConfig, RefOwner, filled_defaults, merge_model
from agentworks.schema.extract import extract_reference_paths
from agentworks.value_provenance import longest_prefix_value

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from agentworks.capabilities.descriptor import Facet
    from agentworks.resources.inheritance import LayerContribution, LayerSource
    from agentworks.source_location import SourceLocation
    from agentworks.value_provenance import ProvenancePath


def merge_activation_layer(
    model: type[BaseModel],
    previous: dict[str, object],
    authored: dict[str, object],
    *,
    facet: Facet,
) -> tuple[dict[str, object], tuple[LayerContribution, ...]]:
    """Merge a host layer and dispatch each activation to its own facet model."""
    from agentworks.resources.inheritance import LayerContribution, LayerContributionKind

    field = "harness_integrations"
    base = cast("Mapping[str, object]", previous.get(field, {}))
    incoming = cast("Mapping[str, object]", authored.get(field, {}))
    merged, host_operations = merge_model(
        model,
        {key: value for key, value in previous.items() if key != field},
        {key: value for key, value in authored.items() if key != field},
    )
    if any(op.kind is LayerContributionKind.RESET_PREFIX and not op.path for op in host_operations):
        base = {}
    activations = {
        name: value if isinstance(value, CapabilityConfig) else CapabilityConfig.model_validate(value)
        for name, value in base.items()
    }
    operations = list(host_operations)
    for name, value in incoming.items():
        config = value.config if isinstance(value, CapabilityConfig) else value
        path = (field, name)
        if value is None:
            activations.pop(name, None)
            operations.append(LayerContribution.reset_prefix(*path))
            if not activations:
                operations.append(LayerContribution.replacement(field))
            continue
        capability_model = capability_config_model("harness-integration", name, facet=facet)
        changes: tuple[LayerContribution, ...]
        if capability_model is None:
            result = config
            changes = (LayerContribution.reset_prefix(*path), LayerContribution.replacement(*path))
        else:
            if name not in activations:
                operations.append(LayerContribution.replacement(*path))
            result, changes = merge_model(
                capability_model, activations[name].config if name in activations else {}, config, path
            )
        operations.extend(changes)
        activations[name] = CapabilityConfig.model_validate(result)
    raw = cast("dict[str, object]", merged)
    raw[field] = activations
    return raw, tuple(operations)


def activation_references(
    activations: Mapping[str, CapabilityConfig],
    *,
    facet: Facet,
    source: tuple[str, str],
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]],
    field: str = "harness_integrations",
) -> tuple[ResourceReference, ...]:
    """Project selected capabilities and attribute config edges to each field's declarer."""
    refs: list[ResourceReference] = []
    for name, config in activations.items():
        prefix = (field, name)
        refs.append(
            ResourceReference(
                kind="harness-integration",
                name=name,
                usage=f"the {facet} harness integration",
                source=source,
                declared_by=_declarer(provenance, prefix),
            )
        )
        model = capability_config_model("harness-integration", name, facet=facet)
        if model is None:
            continue
        blob = filled_defaults(model, config.config, RefOwner(kind=source[0], name=source[1]))
        for path, reference in extract_reference_paths(model, blob):
            refs.extend(sourced_references((reference,), source, _declarer(provenance, (*prefix, *path))))
    return tuple(refs)


def validate_activations(
    activations: Mapping[str, CapabilityConfig],
    *,
    facet: Facet,
    source: tuple[str, str],
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]],
    location: SourceLocation | None = None,
    field: str = "harness_integrations",
) -> None:
    """Validate effective operator-authored configs with per-field declaring owners."""
    for name, config in activations.items():
        prefix = (field, name)
        local: dict[ProvenancePath, RefOwner] = {}
        declaring = _declarer(provenance, prefix)
        if declaring is not None:
            local[()] = _owner(declaring, field, name)
        for path, layers in provenance.items():
            if layers and path[: len(prefix)] == prefix:
                last = layers[-1]
                local[path[len(prefix) :]] = _owner((last.resource_kind, last.name), field, name)
        validate_capability_config(
            kind="harness-integration",
            name=name,
            facet=facet,
            config=config.config,
            owner=_owner(source, field, name),
            provenance=local,
            location=location,
        )


def _declarer(
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]],
    path: ProvenancePath,
) -> tuple[str, str] | None:
    layers = longest_prefix_value(provenance, path) or ()
    return (layers[-1].resource_kind, layers[-1].name) if layers else None


def _owner(source: tuple[str, str], field: str, name: str) -> RefOwner:
    return RefOwner(kind=source[0], name=source[1], label=f"{source[0]}/{source[1]}.{field}.{name}")
