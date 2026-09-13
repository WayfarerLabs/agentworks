"""The ordinary artifact-bundle declared resource."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from agentworks.artifacts.declarations import AgentArtifactSpec, HintArtifactSpec, RuleArtifactSpec, SkillArtifactSpec
from agentworks.declared_resource import DeclaredResource
from agentworks.schema import ResourceRef
from agentworks.schema.reference import RefRelationship

if TYPE_CHECKING:
    from agentworks.resources.graph import FinalizeContext
    from agentworks.resources.inheritance import LayeredResolution
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.value_provenance import LayerContribution

ArtifactName = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)]


class ArtifactBundle(DeclaredResource):
    """Named artifact definitions; each type map merges by key with whole-entry replacement."""

    inherits: list[
        Annotated[
            str, ResourceRef(kind="artifact-bundle", usage="a parent bundle", relationship=RefRelationship.INHERITS)
        ]
    ] = Field(default_factory=list)
    """Parent bundles composed in order, nearest last."""

    hints: dict[ArtifactName, HintArtifactSpec] = Field(default_factory=dict)
    """Small setup facts, keyed by canonical artifact name."""

    rules: dict[ArtifactName, RuleArtifactSpec] = Field(default_factory=dict)
    """Unconditional instructions, keyed by canonical artifact name."""

    skills: dict[ArtifactName, SkillArtifactSpec] = Field(default_factory=dict)
    """Complete skill directories; each key must match the SKILL.md name."""

    agents: dict[ArtifactName, AgentArtifactSpec] = Field(default_factory=dict)
    """Agent personas; each key must match the definition's frontmatter name."""

    def dependencies(self, context: FinalizeContext) -> list[ResourceReference]:
        from agentworks.resources.reference import inherits_reference

        return [inherits_reference(parent, ("artifact-bundle", self.name)) for parent in self.inherits]


def resolve_bundle(registry: Registry, name: str) -> LayeredResolution[ArtifactBundle]:
    """Resolve declared bundles with the shared inheritance order and schema merge."""
    from agentworks.resources.inheritance import (
        DeclarationLayer,
        LayeredResolution,
        LayerSource,
        LayerSourceKind,
        resolution_layers,
        run_layer_fold,
    )
    from agentworks.resources.resolved_spec import resolved_spec_default_paths
    from agentworks.schema import merge_model

    rows = dict(registry.iter_kind_items("artifact-bundle"))
    if name not in rows:
        raise KeyError(name)
    layers = resolution_layers(rows, name, "artifact-bundle")
    # Missing references remain errors even when called without registry finalization.
    for layer in layers:
        for parent in layer.inherits:
            if parent not in rows:
                raise KeyError(parent)
    declared = (
        DeclarationLayer(LayerSource(LayerSourceKind.TEMPLATE, "artifact-bundle", layer.name), layer)
        for layer in layers
    )

    def merge(
        previous: dict[str, object], incoming: ArtifactBundle, source: LayerSource
    ) -> tuple[dict[str, object], tuple[LayerContribution, ...]]:
        merged, changes = merge_model(
            ArtifactBundle,
            previous,
            incoming.model_dump(exclude_unset=True, exclude={"name", "declared_at", "origin", "inherits"}),
        )

        assert isinstance(merged, dict)
        return merged, changes

    folded: LayeredResolution[dict[str, object]] = run_layer_fold(
        {},
        declared,
        merge,
        default_paths=resolved_spec_default_paths(ArtifactBundle),
        default_resource_kind="artifact-bundle",
    )
    value = ArtifactBundle.model_validate({**folded.value, "name": name})
    return LayeredResolution(value, folded.provenance)
