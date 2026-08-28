"""Session template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in default template fallback. Follows the same pattern as VM,
workspace, and agent templates.

Two entry points over one walk, because the framework and the session
resolver want the merge at different moments and in different shapes:

- :func:`effective_template` is the FINALIZE view. Total (it never
  raises), and it keeps the harness pair exactly as the lineage declared
  it, ``None`` and all, because "nobody named an integration" is a
  different edge set from "somebody named ``shell``".
- :func:`resolve_template` / :func:`resolve_from_dict` are the USE view:
  the same merge, collapsed onto :class:`ResolvedSessionTemplate` with
  the ``shell`` default applied, which is what a session is built from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from agentworks.errors import unknown_template_error
from agentworks.schema import RefOwner

#: The workload a session runs when nothing in its lineage names one: a
#: plain login shell, which is the behavior from before harness
#: integrations existed.
DEFAULT_HARNESS_INTEGRATION = "shell"

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.db import Database
    from agentworks.env import EnvEntry
    from agentworks.resources.inheritance import LayerContribution, LayeredResolution, LayerSource
    from agentworks.resources.registry import Registry
    from agentworks.sessions.template import SessionTemplate


@dataclass
class ResolvedSessionTemplate:
    """A fully resolved session template with all inheritance applied.

    The workload is the ``(harness_integration, harness_integration_config)`` pair:
    ``harness_integration`` is always a concrete name (defaulting to ``shell``, the
    plain login shell) and ``harness_integration_config`` is the merged blob the
    session node hands the harness integration. ``description`` stays an
    independently merged display field with a "Login shell" default, unaffected by the pair.
    """

    name: str
    description: str = "Login shell"
    env: dict[str, EnvEntry] = field(default_factory=dict)
    harness_integration: str = DEFAULT_HARNESS_INTEGRATION
    harness_integration_config: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MergedHarness:
    """The ``(harness_integration, config)`` pair a lineage merged to, and
    where each surviving key came from.

    ``name`` is the integration the LINEAGE declared, or ``None`` when no
    template in it declared one. The distinction is load-bearing here in a
    way it is not on :class:`ResolvedSessionTemplate`: a silent lineage
    must publish no ``harness-integration`` edge at all, whereas collapsing
    it to ``shell`` first would have every session template in the registry
    pointing at the shell row.

    ``provenance`` maps each key of ``config`` to the template that
    declared the value that survived the merge, so a shape error on an
    inherited key can name the template an operator has to go and edit
    rather than the child that merely inherited it. It records the LAST
    declarer of a key in merge order, which is the one child-wins keeps;
    for a key an integration's ``merge_config`` COMBINES across layers
    (``shell`` unions ``required_commands``) the last declarer is one
    contributor among several, and the message says "inherited from" rather
    than claiming sole authorship.

    ``declared_by`` is the same fact at BLOCK granularity, for the
    reference path: an edge implied by this config, and the selector edge
    itself, belong to the layer that named the integration, because
    switching integration is what discards accumulated config. The two
    coexist rather than one deriving from the other: a validation error
    names a KEY and can be that precise, while a reference carries no key
    to be precise about.
    """

    name: str | None = None
    config: Mapping[str, object] = MappingProxyType({})
    provenance: Mapping[str, RefOwner] = MappingProxyType({})
    declared_by: tuple[str, str] | None = None


@dataclass(frozen=True)
class EffectiveSessionTemplate:
    """One session template's chain, merged, as the finalize passes need it."""

    resolved: ResolvedSessionTemplate
    harness: MergedHarness = MergedHarness()


@dataclass
class _SessionAccumulator:
    resolved: ResolvedSessionTemplate
    harness: MergedHarness


def _merge_map(target: dict[str, EnvEntry], source: dict[str, EnvEntry]) -> dict[str, EnvEntry]:
    """Merge source env map into target. Source wins on key collision."""
    return {**target, **source}


def resolve_from_dict(
    templates: dict[str, SessionTemplate],
    template_name: str | None = None,
    *,
    overlay: SessionTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedSessionTemplate:
    """Resolve a session template from a templates dict (no Config required)."""
    return resolve_from_dict_with_provenance(
        templates,
        template_name,
        overlay=overlay,
        instance_name=instance_name,
    ).value


def resolve_from_dict_with_provenance(
    templates: dict[str, SessionTemplate],
    template_name: str | None = None,
    *,
    overlay: SessionTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedSessionTemplate]:
    if template_name is not None and template_name != "default":
        if template_name not in templates:
            raise unknown_template_error(
                kind="session-template",
                label="session template",
                name=template_name,
                available=templates,
            )
        return _resolve_with_provenance(templates, template_name, overlay=overlay, instance_name=instance_name)

    if "default" in templates:
        return _resolve_with_provenance(templates, "default", overlay=overlay, instance_name=instance_name)

    return _resolve_with_provenance({}, "default", overlay=overlay, instance_name=instance_name)


def resolve_template(
    registry: Registry,
    template_name: str | None = None,
    *,
    overlay: SessionTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedSessionTemplate:
    """Resolve a session template by name, applying inheritance."""

    return resolve_template_with_provenance(
        registry,
        template_name,
        overlay=overlay,
        instance_name=instance_name,
    ).value


def resolve_template_with_provenance(
    registry: Registry,
    template_name: str | None = None,
    *,
    overlay: SessionTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedSessionTemplate]:
    from agentworks.resources.access import kind_dict

    return resolve_from_dict_with_provenance(
        kind_dict(registry, "session-template"),
        template_name,
        overlay=overlay,
        instance_name=instance_name,
    )


def resolve_live_template(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> ResolvedSessionTemplate:
    """Resolve a persisted session's template chain plus its stored final layer."""
    from typing import cast

    from agentworks.instance_specs import get_instance_overlay

    overlay = get_instance_overlay(db, "session", instance_name)
    if overlay is None:
        return resolve_template(registry, template_name)
    return resolve_template(
        registry,
        template_name,
        overlay=cast("SessionTemplate", overlay.declaration),
        instance_name=instance_name,
    )


def resolve_live_template_with_provenance(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> LayeredResolution[ResolvedSessionTemplate]:
    """Resolve a persisted session and retain its layer provenance."""
    from typing import cast

    from agentworks.instance_specs import get_instance_overlay

    overlay = get_instance_overlay(db, "session", instance_name)
    return resolve_template_with_provenance(
        registry,
        template_name,
        overlay=None if overlay is None else cast("SessionTemplate", overlay.declaration),
        instance_name=instance_name,
    )


def effective_template(templates: Mapping[str, SessionTemplate], name: str) -> EffectiveSessionTemplate:
    """The effective (merged) declaration of ``name``, for the finalize
    passes. TOTAL; see ``vms/templates.effective_template`` for why the
    degradation on a cyclic chain is never observed."""
    return effective_template_with_provenance(templates, name).value


def effective_template_with_provenance(
    templates: Mapping[str, SessionTemplate],
    name: str,
) -> LayeredResolution[EffectiveSessionTemplate]:
    """The total finalize view with the provenance of every surviving value."""
    from agentworks.errors import InheritanceCycleError
    from agentworks.resources.inheritance import LayeredResolution

    try:
        layered = _resolve_walk_with_provenance(templates, name)
    except InheritanceCycleError:
        return LayeredResolution(EffectiveSessionTemplate(resolved=ResolvedSessionTemplate(name=name)), {})
    return LayeredResolution(
        EffectiveSessionTemplate(resolved=layered.value.resolved, harness=layered.value.harness),
        layered.provenance,
    )


def _resolve(
    templates: Mapping[str, SessionTemplate],
    name: str,
    *,
    overlay: SessionTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedSessionTemplate:
    """Depth-first, left-to-right resolution.

    The USE view over :func:`_resolve_walk`: it collapses the walk's
    ``(harness_integration | None, config)`` pair onto the dataclass, an
    undeclared pair becoming the ``shell`` default. No validation happens
    here: the merged blob's shape check runs at finalize over this same
    merge, with the rest of hard validation (FR12), and construction
    re-validates the blob it binds.
    """
    return _resolve_with_provenance(
        templates,
        name,
        overlay=overlay,
        instance_name=instance_name,
    ).value


def _resolve_with_provenance(
    templates: Mapping[str, SessionTemplate],
    name: str,
    *,
    overlay: SessionTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedSessionTemplate]:
    from agentworks.resources.inheritance import LayeredResolution

    layered = _resolve_walk_with_provenance(
        templates,
        name,
        overlay=overlay,
        instance_name=instance_name,
    )
    result = layered.value.resolved
    result.harness_integration = layered.value.harness.name or DEFAULT_HARNESS_INTEGRATION
    result.harness_integration_config = dict(layered.value.harness.config)
    return LayeredResolution(result, layered.provenance)


def _declared_pair(tmpl: SessionTemplate, source: LayerSource | None = None) -> MergedHarness:
    """One template's OWN harness declaration, in the shape the fold
    takes. ``name`` is ``None`` when this template declares no
    integration, which is what :func:`_merge_pair` reads as "says nothing"
    and leaves the accumulator alone for.
    """
    block = tmpl.harness_integration
    if block is None:
        return MergedHarness()
    owner_kind = "session-template" if source is None else source.resource_kind
    owner_name = tmpl.name if source is None else source.name
    owner = RefOwner(kind=owner_kind, name=owner_name)
    return MergedHarness(
        name=block.name,
        config=block.config,
        provenance=dict.fromkeys(block.config, owner),
        declared_by=(owner_kind, owner_name),
    )


def _resolve_walk(
    templates: Mapping[str, SessionTemplate],
    name: str,
    *,
    overlay: SessionTemplate | None = None,
    instance_name: str | None = None,
) -> EffectiveSessionTemplate:
    """Resolve ``name``'s chain: one accumulator per half, folded over the
    chain's declarations in merge order.

    Both halves fold the same layer list, so the pair and the
    ``description`` / ``env`` half can no longer disagree about which
    declaration came last. That the fold reads the DECLARATIONS rather
    than each parent's already-resolved template is what keeps a silent
    layer silent; see ``vms.templates._resolve_from_dict``.

    A ``None`` integration name means nothing in the lineage declared one
    (distinct from a declared ``shell``): keeping that distinction is what
    lets a selector-silent later parent leave an earlier parent's pair
    untouched instead of switching the lineage back to ``shell``.

    Folding FLAT is also what makes an integration switch stick. A chain
    that names A, switches to B, then names A again keeps only what the
    last A declared, because the switch to B discarded A's blob and
    nothing brings it back. Merging each parent's already-merged pair
    would hide that switch inside the parent's own result and resurrect
    the first A's config across a capability that had already discarded
    it, contradicting the rule :func:`_merge_pair` documents. Pinned by
    ``tests/sessions/test_session_template_surface.py``.
    """
    layered = _resolve_walk_with_provenance(
        templates,
        name,
        overlay=overlay,
        instance_name=instance_name,
    )
    return EffectiveSessionTemplate(resolved=layered.value.resolved, harness=layered.value.harness)


def _resolve_walk_with_provenance(
    templates: Mapping[str, SessionTemplate],
    name: str,
    *,
    overlay: SessionTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[_SessionAccumulator]:
    # Imported here, not at module level: ``agentworks.resources``'s package
    # init loads every kind module, and every kind module reaches this one.
    from agentworks.resources.inheritance import (
        DeclarationLayer,
        LayerSource,
        LayerSourceKind,
        resolution_layers,
        run_layer_fold,
    )

    layers = [
        DeclarationLayer(
            LayerSource(LayerSourceKind.TEMPLATE, "session-template", layer.name),
            layer,
        )
        for layer in resolution_layers(templates, name, "session-template")
    ]
    if overlay is not None:
        layers.append(
            DeclarationLayer(
                LayerSource(LayerSourceKind.INSTANCE, "session", instance_name or overlay.name),
                overlay,
            )
        )
    return run_layer_fold(
        _SessionAccumulator(ResolvedSessionTemplate(name=name), MergedHarness()),
        layers,
        _merge_session_layer,
        default_paths=(("description",), ("harness_integration",)),
        default_resource_kind="session-template",
        default_name=name,
    )


def _merge_session_layer(
    target: _SessionAccumulator,
    tmpl: SessionTemplate,
    source: LayerSource,
) -> tuple[_SessionAccumulator, tuple[LayerContribution, ...]]:
    from agentworks.resources.inheritance import LayerContribution

    target.resolved, template_paths = _merge_template(target.resolved, tmpl, source)
    declared = _declared_pair(tmpl, source)
    same_integration = declared.name == target.harness.name
    previous_config = target.harness.config
    target.harness = _merge_pair(target.harness, declared)
    harness_paths: list[LayerContribution] = []
    if declared.name is not None:
        harness_paths.append(LayerContribution.replacement("harness_integration"))
        if not same_integration:
            harness_paths.append(LayerContribution.reset_prefix("harness_integration_config"))
        else:
            for key in previous_config:
                if key not in target.harness.config:
                    harness_paths.append(LayerContribution.reset_prefix("harness_integration_config", key))
        for key, value in declared.config.items():
            merged_value = target.harness.config.get(key)
            if isinstance(value, list) and isinstance(merged_value, list):
                previous_value = previous_config.get(key)
                additive = (
                    same_integration
                    and isinstance(previous_value, list)
                    and _list_merge_preserves_base(declared.name, key, value)
                )
                if not additive:
                    harness_paths.append(LayerContribution.reset_prefix("harness_integration_config", key))
                    contributed = merged_value
                else:
                    assert isinstance(previous_value, list)
                    surviving = {repr(item) for item in merged_value}
                    harness_paths.extend(
                        LayerContribution.reset_prefix("harness_integration_config", key, repr(item))
                        for item in previous_value
                        if repr(item) not in surviving
                    )
                    contributed = [item for item in value if repr(item) in surviving]
                harness_paths.extend(
                    LayerContribution.contribution("harness_integration_config", key, repr(item))
                    for item in contributed
                )
            elif key in target.harness.config:
                if isinstance(previous_config.get(key), list):
                    harness_paths.append(LayerContribution.reset_prefix("harness_integration_config", key))
                harness_paths.append(LayerContribution.replacement("harness_integration_config", key))
    return target, (*template_paths, *harness_paths)


def _list_merge_preserves_base(integration_name: str, key: str, child: list[object]) -> bool:
    """Ask the integration's reducer whether this list key accumulates."""
    from agentworks.capabilities.harness_integration import merged_config

    sentinel = "\x00agentworks-provenance-sentinel"
    while sentinel in child:
        sentinel += "-next"
    probe = merged_config(integration_name, {key: [sentinel]}, {key: child})
    merged = probe.get(key)
    return isinstance(merged, list) and sentinel in merged


def _merge_pair(acc: MergedHarness, child: MergedHarness) -> MergedHarness:
    """Fold one layer's DECLARED harness pair into the accumulator:

    - a child that says nothing about the harness integration leaves the pair
      untouched (a ``harness_integration_config`` without a ``harness_integration`` cannot load,
      so silence is unambiguous);
    - a child naming a DIFFERENT harness integration starts from a fresh blob (the
      parent's blob was addressed to the wrong capability, never leaks),
      and from fresh provenance with it;
    - a child naming the SAME harness integration merges via that integration's
      ``merge_config`` (child-wins per key; ``shell`` unions
      ``required_commands``).

    Reaches the merge by NAME (``merged_config``), which is what keeps
    this total for the finalize walk: an unregistered name is a dangling
    edge the miss policy reports, not a reason for the merge to raise.

    Provenance follows the value: it is restricted to the keys that
    actually survived, so a key an integration's merge dropped cannot be
    blamed on the template that declared it.
    """
    if child.name is None:
        return acc
    from agentworks.capabilities.harness_integration import merged_config

    same = child.name == acc.name
    base = acc.config if same else {}
    config = merged_config(child.name, base, child.config)
    provenance = {**(acc.provenance if same else {}), **child.provenance}
    return MergedHarness(
        name=child.name,
        config=config,
        provenance={key: owner for key, owner in provenance.items() if key in config},
        declared_by=child.declared_by,
    )


def _merge_template(
    target: ResolvedSessionTemplate,
    tmpl: SessionTemplate,
    _source: object,
) -> tuple[ResolvedSessionTemplate, tuple[LayerContribution, ...]]:
    """Fold one declared SessionTemplate's description / env into the
    accumulator (the pair folds separately, via :func:`_merge_pair`).
    None = not set, skip. Scalars: later layer overrides. Maps: merge with
    the later layer winning. The only writer of those two fields."""
    from agentworks.resources.inheritance import LayerContribution

    touched: list[LayerContribution] = []
    if tmpl.description is not None:
        target.description = tmpl.description
        touched.append(LayerContribution.replacement("description"))
    if tmpl.env:
        target.env = _merge_map(target.env, tmpl.env)
        touched.extend(LayerContribution.replacement("env", key) for key in tmpl.env)
    return target, tuple(touched)
