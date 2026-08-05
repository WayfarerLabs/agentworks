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

    The workload is the ``(harness_integration, harness_integration_config)`` pair:
    ``harness_integration`` is always a concrete name (defaulting to ``shell``, the
    plain login shell) and ``harness_integration_config`` is the merged blob the
    session node hands the harness integration. ``description`` stays an
    independently merged display field with a "Login shell" default, unaffected by the pair.
    """

    name: str
    description: str = "Login shell"
    env: dict[str, EnvEntry] = field(default_factory=dict)
    harness_integration: str = "shell"
    harness_integration_config: dict[str, object] = field(default_factory=dict)


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
    walk's running ``(harness_integration | None, config)`` pair onto the dataclass
    (an undeclared pair becomes the ``shell`` default) and runs the
    harness integration's completeness validation once on the merged blob, the value
    no single declaration saw.
    """
    result, harness_integration, harness_integration_config = _resolve_walk(templates, name, _visiting)
    result.harness_integration = harness_integration or "shell"
    result.harness_integration_config = harness_integration_config
    _validate_merged(result)
    return result


def _resolve_walk(
    templates: dict[str, SessionTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
) -> tuple[ResolvedSessionTemplate, str | None, dict[str, object]]:
    """Depth-first, left-to-right resolution, threading the raw harness_integration
    pair alongside the accumulating ``ResolvedSessionTemplate``.

    Returns ``(result, harness_integration | None, harness_integration_config)`` where a ``None``
    integration name means nothing in the lineage declared one (distinct from a
    declared ``shell``): keeping that distinction is what lets a
    selector-silent later parent leave an earlier parent's pair untouched
    instead of switching the lineage back to ``shell``. ``description`` / ``env`` merge exactly as
    before, independent of the pair.

    ``_visiting`` carries the chain of in-progress resolves so cycles
    raise ``ConfigError`` instead of ``RecursionError``. The framework's
    cycle pass at build_registry time is the canonical check; this guard
    is the safety net for callers that resolve without going through
    build_registry.
    """
    if name in _visiting:
        path = " -> ".join((*_visiting, name))
        raise ConfigError(f"session_templates inheritance cycle detected: {path}")

    if name not in templates:
        return ResolvedSessionTemplate(name=name), None, {}

    tmpl = templates[name]
    result = ResolvedSessionTemplate(name=name)
    harness_integration: str | None = None
    harness_integration_config: dict[str, object] = {}
    next_visiting = (*_visiting, name)

    for parent_name in tmpl.inherits:
        parent, parent_harness_integration, parent_config = _resolve_walk(templates, parent_name, next_visiting)
        _merge(result, parent)
        harness_integration, harness_integration_config = _merge_pair(
            harness_integration,
            harness_integration_config,
            parent_harness_integration,
            parent_config,
        )

    _merge_template(result, tmpl)
    harness_integration, harness_integration_config = _merge_pair(
        harness_integration,
        harness_integration_config,
        tmpl.harness_integration,
        tmpl.harness_integration_config,
    )
    result.name = name
    return result, harness_integration, harness_integration_config


def _merge_pair(
    acc_harness_integration: str | None,
    acc_config: dict[str, object],
    child_harness_integration: str | None,
    child_config: dict[str, object] | None,
) -> tuple[str | None, dict[str, object]]:
    """Fold one declared (or resolved) ``(harness_integration, config)`` into the
    accumulator:

    - a child that says nothing about the harness integration leaves the pair
      untouched (a ``harness_integration_config`` without a ``harness_integration`` cannot load,
      so silence is unambiguous);
    - a child naming a DIFFERENT harness integration starts from a fresh blob (the
      parent's blob was addressed to the wrong capability, never leaks);
    - a child naming the SAME harness integration merges via that integration's
      ``merge_config`` (child-wins per key; ``shell`` unions
      ``required_commands``).
    """
    if child_harness_integration is None:
        return acc_harness_integration, acc_config
    from agentworks.capabilities.harness_integration import harness_integration_for

    base = acc_config if child_harness_integration == acc_harness_integration else {}
    merged = harness_integration_for(child_harness_integration).merge_config(base, child_config or {})
    return child_harness_integration, merged


def _validate_merged(resolved: ResolvedSessionTemplate) -> None:
    """Run the selected harness integration's completeness validation on the merged
    blob. The shipped integrations are shape-only, but
    the slot is where a future integration's required-field / cross-field
    rules belong. ``harness_integration_for`` raises a typed ``ConfigError`` on an
    unknown name (defense in depth; typos are normally caught by the
    kind's miss policy at finalize)."""
    from agentworks.capabilities.harness_integration import harness_integration_for

    harness_integration_for(resolved.harness_integration).validate(
        f"session-template/{resolved.name}", resolved.harness_integration_config
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
