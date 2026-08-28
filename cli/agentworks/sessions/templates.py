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
from typing import TYPE_CHECKING, cast

from agentworks.errors import unknown_template_error
from agentworks.schema import merge_model

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
    """The ``(harness_integration, config)`` pair a lineage merged to.

    ``name`` is the integration the LINEAGE declared, or ``None`` when no
    template in it declared one. The distinction is load-bearing here in a
    way it is not on :class:`ResolvedSessionTemplate`: a silent lineage
    must publish no ``harness-integration`` edge at all, whereas collapsing
    it to ``shell`` first would have every session template in the registry
    pointing at the shell row.

    Value ownership lives exclusively in the surrounding
    :class:`LayeredResolution`. Keeping a second map here would let the two
    provenance answers drift.
    """

    name: str | None = None
    config: Mapping[str, object] = MappingProxyType({})


@dataclass(frozen=True)
class EffectiveSessionTemplate:
    """One session template's chain, merged, as the finalize passes need it."""

    resolved: ResolvedSessionTemplate
    harness: MergedHarness = MergedHarness()


@dataclass
class _SessionAccumulator:
    resolved: ResolvedSessionTemplate
    harness: MergedHarness


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


def _declared_pair(tmpl: SessionTemplate) -> MergedHarness:
    """One template's OWN harness declaration, in the shape the fold
    takes. ``name`` is ``None`` when this template declares no integration.
    """
    block = tmpl.harness_integration
    if block is None:
        return MergedHarness()
    return MergedHarness(name=block.name, config=block.config)


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
    it, contradicting the selector transition below. Pinned by
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
    target.resolved, template_paths = _merge_template(target.resolved, tmpl, source)
    declared = _declared_pair(tmpl)
    if declared.name is None:
        return target, template_paths

    from agentworks.capabilities.config import capability_config_model
    from agentworks.resources.inheritance import LayerContribution

    config_path = ("harness_integration_config",)
    same_integration = declared.name == target.harness.name
    model = capability_config_model("harness-integration", declared.name)
    harness_paths: list[LayerContribution] = [LayerContribution.replacement("harness_integration")]

    if model is None:
        # A missing capability has no schema under which two declarations
        # could honestly compose. Keep the finalize walk total and let the
        # kind's Registry miss report the selector.
        harness_paths.extend(
            (
                LayerContribution.reset_prefix(*config_path),
                LayerContribution.replacement(*config_path),
            )
        )
        target.harness = MergedHarness(declared.name, dict(declared.config))
        return target, (*template_paths, *harness_paths)

    base: object = target.harness.config if same_integration else {}
    merged, operations = merge_model(model, base, declared.config, config_path)
    if not same_integration:
        harness_paths.extend(
            (
                LayerContribution.reset_prefix(*config_path),
                LayerContribution.replacement(*config_path),
            )
        )
    harness_paths.extend(operations)
    target.harness = MergedHarness(declared.name, cast("dict[str, object]", merged))
    return target, (*template_paths, *harness_paths)


def _merge_template(
    target: ResolvedSessionTemplate,
    tmpl: SessionTemplate,
    _source: object,
) -> tuple[ResolvedSessionTemplate, tuple[LayerContribution, ...]]:
    """Merge the session-owned fields after applying authored absence rules."""
    from agentworks.env.entry import EnvEntry

    previous: dict[str, object] = {
        "description": target.description,
        "env": {key: entry.model_dump(mode="python") for key, entry in target.env.items()},
    }
    incoming: dict[str, object] = {}
    if tmpl.description is not None:
        incoming["description"] = tmpl.description
    if tmpl.env:
        incoming["env"] = {key: entry.model_dump(mode="python") for key, entry in tmpl.env.items()}
    merged, operations = merge_model(type(tmpl), previous, incoming)
    raw = cast("dict[str, object]", merged)
    target.description = cast("str", raw["description"])
    target.env = {key: EnvEntry.model_validate(value) for key, value in cast("dict[str, object]", raw["env"]).items()}
    return target, operations
