"""Shared value-provenance operations and layer-fold behavior."""

from __future__ import annotations

from agentworks.resources.inheritance import (
    DeclarationLayer,
    LayerContribution,
    LayerContributionKind,
    LayerSource,
    LayerSourceKind,
    run_layer_fold,
)
from agentworks.value_provenance import (
    LayerContribution as LeafLayerContribution,
)
from agentworks.value_provenance import (
    LayerContributionKind as LeafLayerContributionKind,
)
from agentworks.value_provenance import longest_prefix_value


def test_resources_reexports_the_leaf_operation_objects() -> None:
    assert LayerContribution is LeafLayerContribution
    assert LayerContributionKind is LeafLayerContributionKind


def test_longest_prefix_includes_the_root_and_prefers_the_narrowest_match() -> None:
    values = {
        (): "root",
        ("config",): "config",
        ("config", "items", 2): "item",
    }

    assert longest_prefix_value(values, ("config", "items", 2, "name")) == "item"
    assert longest_prefix_value(values, ("config", "other")) == "config"
    assert longest_prefix_value(values, ("other",)) == "root"


def test_a_new_contribution_seeds_sources_from_its_longest_prefix() -> None:
    parent = LayerSource(LayerSourceKind.TEMPLATE, "agent-template", "parent")
    child = LayerSource(LayerSourceKind.TEMPLATE, "agent-template", "child")
    layers = (
        DeclarationLayer(parent, "parent"),
        DeclarationLayer(child, "child"),
    )

    def reducer(value: str, layer: str, _source: LayerSource):
        if layer == "parent":
            return value, (LayerContribution.replacement("items"),)
        return value, (LayerContribution.contribution("items", 0),)

    resolution = run_layer_fold(
        "value",
        layers,
        reducer,
        default_resource_kind="agent-template",
    )

    assert resolution.provenance == {
        ("items",): (parent,),
        ("items", 0): (parent, child),
    }


def test_prefix_reset_discards_descendants_before_replacement() -> None:
    old = LayerSource(LayerSourceKind.TEMPLATE, "agent-template", "old")
    new = LayerSource(LayerSourceKind.TEMPLATE, "agent-template", "new")

    def reducer(value: str, layer: str, _source: LayerSource):
        if layer == "old":
            return value, (LayerContribution.replacement("block", "child"),)
        return value, (
            LayerContribution.reset_prefix("block"),
            LayerContribution.replacement("block"),
        )

    resolution = run_layer_fold(
        "value",
        (DeclarationLayer(old, "old"), DeclarationLayer(new, "new")),
        reducer,
        default_resource_kind="agent-template",
    )

    assert resolution.provenance == {("block",): (new,)}


def test_a_new_list_index_replacement_does_not_inherit_the_list_owner() -> None:
    parent = LayerSource(LayerSourceKind.TEMPLATE, "agent-template", "parent")
    child = LayerSource(LayerSourceKind.TEMPLATE, "agent-template", "child")

    def reducer(value: str, layer: str, _source: LayerSource):
        operation = (
            LayerContribution.replacement("items") if layer == "parent" else LayerContribution.replacement("items", 0)
        )
        return value, (operation,)

    resolution = run_layer_fold(
        "value",
        (DeclarationLayer(parent, "parent"), DeclarationLayer(child, "child")),
        reducer,
        default_resource_kind="agent-template",
    )

    assert resolution.provenance[("items", 0)] == (child,)
