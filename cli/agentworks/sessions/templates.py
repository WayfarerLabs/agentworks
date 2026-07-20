"""Session template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in default template fallback. Follows the same pattern as VM,
workspace, and agent templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError, unknown_template_error

if TYPE_CHECKING:
    from agentworks.env import EnvEntry
    from agentworks.resources.registry import Registry
    from agentworks.sessions.template import SessionTemplate


@dataclass
class ResolvedSessionTemplate:
    """A fully resolved session template with all inheritance applied.

    The workload is the ``(harness, harness_config)`` pair (FRD R7):
    ``harness`` is always a concrete name (defaulting to ``shell``, the
    plain login shell) and ``harness_config`` is the merged blob the
    session node hands the harness. ``description`` stays an
    independently merged display field (pinned "Login shell" default,
    harness-api-lld section 10), unaffected by the pair.
    """

    name: str
    description: str = "Login shell"
    env: dict[str, EnvEntry] = field(default_factory=dict)
    harness: str = "shell"
    harness_config: dict[str, object] = field(default_factory=dict)


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


def _resolve(
    templates: dict[str, SessionTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
) -> ResolvedSessionTemplate:
    """Depth-first, left-to-right resolution.

    The public wrapper over :func:`_resolve_walk`: it collapses the
    walk's running ``(harness | None, config)`` pair onto the dataclass
    (an undeclared pair becomes the ``shell`` default) and runs the
    harness's completeness validation once on the MERGED blob, the value
    no single declaration saw (harness-api-lld section 2).
    """
    result, harness, harness_config = _resolve_walk(templates, name, _visiting)
    result.harness = harness or "shell"
    result.harness_config = harness_config
    _validate_merged(result)
    return result


def _resolve_walk(
    templates: dict[str, SessionTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
) -> tuple[ResolvedSessionTemplate, str | None, dict[str, object]]:
    """Depth-first, left-to-right resolution, threading the raw harness
    pair alongside the accumulating ``ResolvedSessionTemplate``.

    Returns ``(result, harness | None, harness_config)`` where a ``None``
    harness means nothing in the lineage declared one (distinct from a
    declared ``shell``): keeping that distinction is what lets a
    harness-silent later parent leave an earlier parent's pair untouched
    instead of switching the lineage back to ``shell`` (FRD R5's
    multi-parent divergence). ``description`` / ``env`` merge exactly as
    before, independent of the pair.

    ``_visiting`` carries the chain of in-progress resolves so cycles
    raise ``ConfigError`` instead of ``RecursionError``. The framework's
    cycle pass at build_registry time is the canonical check; this guard
    is the safety net for callers that resolve without going through
    build_registry.
    """
    if name in _visiting:
        path = " -> ".join((*_visiting, name))
        raise ConfigError(
            f"session_templates inheritance cycle detected: {path}"
        )

    if name not in templates:
        return ResolvedSessionTemplate(name=name), None, {}

    tmpl = templates[name]
    result = ResolvedSessionTemplate(name=name)
    harness: str | None = None
    harness_config: dict[str, object] = {}
    next_visiting = (*_visiting, name)

    for parent_name in tmpl.inherits:
        parent, parent_harness, parent_config = _resolve_walk(
            templates, parent_name, next_visiting
        )
        _merge(result, parent)
        harness, harness_config = _merge_pair(
            harness, harness_config, parent_harness, parent_config
        )

    _merge_template(result, tmpl)
    harness, harness_config = _merge_pair(
        harness, harness_config, tmpl.harness, tmpl.harness_config
    )
    result.name = name
    return result, harness, harness_config


def _merge_pair(
    acc_harness: str | None,
    acc_config: dict[str, object],
    child_harness: str | None,
    child_config: dict[str, object] | None,
) -> tuple[str | None, dict[str, object]]:
    """Fold one declared (or resolved) ``(harness, config)`` into the
    accumulator (FRD R5, harness-api-lld section 9):

    - a child that says nothing about the harness leaves the pair
      untouched (a ``harness_config`` without a ``harness`` cannot load,
      so silence is unambiguous);
    - a child naming a DIFFERENT harness starts from a fresh blob (the
      parent's blob was addressed to the wrong capability, never leaks);
    - a child naming the SAME harness merges via that harness's
      ``merge_config`` (child-wins per key; ``shell`` unions
      ``required_commands``).
    """
    if child_harness is None:
        return acc_harness, acc_config
    from agentworks.capabilities.harness import harness_for

    base = acc_config if child_harness == acc_harness else {}
    merged = harness_for(child_harness).merge_config(base, child_config or {})
    return child_harness, merged


def _validate_merged(resolved: ResolvedSessionTemplate) -> None:
    """Run the selected harness's completeness validation on the merged
    blob (harness-api-lld section 2). Both built-ins are shape-only, but
    the slot is where a future harness's required-field / cross-field
    rules belong. ``harness_for`` raises a typed ``ConfigError`` on an
    unknown name (defense in depth; typos are normally caught by the
    kind's miss policy at finalize)."""
    from agentworks.capabilities.harness import harness_for

    harness_for(resolved.harness).validate_config(
        f"session-template/{resolved.name}", resolved.harness_config
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
