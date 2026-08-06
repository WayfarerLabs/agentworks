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

from agentworks.errors import inheritance_cycle_error, unknown_template_error
from agentworks.schema import RefOwner

#: The workload a session runs when nothing in its lineage names one: a
#: plain login shell, which is the behavior from before harness
#: integrations existed.
DEFAULT_HARNESS_INTEGRATION = "shell"

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.env import EnvEntry
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


def _merge_map(target: dict[str, EnvEntry], source: dict[str, EnvEntry]) -> dict[str, EnvEntry]:
    """Merge source env map into target. Source wins on key collision."""
    return {**target, **source}


def resolve_from_dict(
    templates: dict[str, SessionTemplate],
    template_name: str | None = None,
) -> ResolvedSessionTemplate:
    """Resolve a session template from a templates dict (no Config required)."""
    if template_name is not None and template_name != "default":
        if template_name not in templates:
            raise unknown_template_error(
                kind="session-template",
                label="session template",
                name=template_name,
                available=templates,
            )
        return _resolve(templates, template_name)

    if "default" in templates:
        return _resolve(templates, "default")

    return ResolvedSessionTemplate(name="default")


def resolve_template(registry: Registry, template_name: str | None = None) -> ResolvedSessionTemplate:
    """Resolve a session template by name, applying inheritance."""
    from agentworks.resources.access import kind_dict

    return resolve_from_dict(kind_dict(registry, "session-template"), template_name)


def effective_template(templates: Mapping[str, SessionTemplate], name: str) -> EffectiveSessionTemplate:
    """The effective (merged) declaration of ``name``, for the finalize
    passes. TOTAL; see ``vms/templates.effective_template`` for why the
    degradation on a cyclic chain is never observed."""
    from agentworks.errors import InheritanceCycleError

    try:
        return _resolve_walk(templates, name)
    except InheritanceCycleError:
        return EffectiveSessionTemplate(resolved=ResolvedSessionTemplate(name=name))


def _resolve(
    templates: Mapping[str, SessionTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
) -> ResolvedSessionTemplate:
    """Depth-first, left-to-right resolution.

    The USE view over :func:`_resolve_walk`: it collapses the walk's
    ``(harness_integration | None, config)`` pair onto the dataclass, an
    undeclared pair becoming the ``shell`` default. No validation happens
    here: the merged blob's shape check runs at finalize over this same
    merge, with the rest of hard validation (FR12), and construction
    re-validates the blob it binds.
    """
    effective = _resolve_walk(templates, name, _visiting)
    result = effective.resolved
    result.harness_integration = effective.harness.name or DEFAULT_HARNESS_INTEGRATION
    result.harness_integration_config = dict(effective.harness.config)
    return result


def _resolve_walk(
    templates: Mapping[str, SessionTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
) -> EffectiveSessionTemplate:
    """Depth-first, left-to-right resolution, threading the raw harness_integration
    pair alongside the accumulating ``ResolvedSessionTemplate``.

    A ``None`` integration name means nothing in the lineage declared one
    (distinct from a declared ``shell``): keeping that distinction is what
    lets a selector-silent later parent leave an earlier parent's pair
    untouched instead of switching the lineage back to ``shell``.
    ``description`` / ``env`` merge exactly as before, independent of the
    pair.

    ``_visiting`` carries the chain of in-progress resolves so cycles
    raise ``InheritanceCycleError`` instead of ``RecursionError``. The
    framework's cycle pass at build_registry time is the canonical check;
    this guard is the safety net for callers that resolve without going
    through build_registry, and :func:`effective_template` keys on the
    type to stay total.
    """
    if name in _visiting:
        raise inheritance_cycle_error("session-template", (*_visiting, name))

    if name not in templates:
        return EffectiveSessionTemplate(resolved=ResolvedSessionTemplate(name=name))

    tmpl = templates[name]
    result = ResolvedSessionTemplate(name=name)
    harness = MergedHarness()
    next_visiting = (*_visiting, name)

    for parent_name in tmpl.inherits:
        parent = _resolve_walk(templates, parent_name, next_visiting)
        _merge(result, parent.resolved)
        harness = _merge_pair(harness, parent.harness)

    _merge_template(result, tmpl)
    declared_harness = tmpl.harness_integration
    declared_config = declared_harness.config if declared_harness is not None else {}
    harness = _merge_pair(
        harness,
        MergedHarness(
            name=declared_harness.name if declared_harness is not None else None,
            config=declared_config,
            provenance=dict.fromkeys(declared_config, RefOwner(kind="session-template", name=name)),
            declared_by=("session-template", name),
        ),
    )
    result.name = name
    return EffectiveSessionTemplate(resolved=result, harness=harness)


def _merge_pair(acc: MergedHarness, child: MergedHarness) -> MergedHarness:
    """Fold one declared (or already-merged) harness pair into the
    accumulator:

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


def _merge(target: ResolvedSessionTemplate, source: ResolvedSessionTemplate) -> None:
    """Merge source's description / env into target (the pair merges
    separately, via :func:`_merge_pair`). Scalars: source wins. Maps:
    merge with source wins."""
    target.description = source.description
    target.env = _merge_map(target.env, source.env)


def _merge_template(target: ResolvedSessionTemplate, tmpl: SessionTemplate) -> None:
    """Merge a raw SessionTemplate's description / env into a
    ResolvedSessionTemplate (the pair merges separately, via
    :func:`_merge_pair`). None = not set, skip. Scalars: child
    overrides. Maps: merge with child wins."""
    if tmpl.description is not None:
        target.description = tmpl.description
    if tmpl.env is not None:
        target.env = _merge_map(target.env, tmpl.env)
