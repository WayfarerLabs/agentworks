"""Workspace template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in empty template fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from agentworks.env import EnvEntry
    from agentworks.resources.registry import Registry
    from agentworks.workspaces.template import WorkspaceTemplate


@dataclass
class ResolvedTemplate:
    """A fully resolved workspace template with all inheritance applied."""

    name: str
    repo: str | None = None
    tmuxinator: bool = True
    env: dict[str, EnvEntry] = field(default_factory=dict)


def resolve_from_dict(
    templates: dict[str, WorkspaceTemplate],
    template_name: str | None = None,
) -> ResolvedTemplate:
    """Resolve a workspace template from a templates dict.

    Selection order:
    1. Explicit template_name
    2. "default" template if it exists
    3. Built-in empty template (tmuxinator=True, no repo)
    """
    if template_name is not None and template_name != "default":
        if template_name not in templates:
            msg = f"Unknown workspace template: {template_name}"
            raise ValueError(msg)
        return _resolve(templates, template_name)

    if "default" in templates:
        return _resolve(templates, "default")

    return ResolvedTemplate(name="default")


def resolve_template(registry: Registry, template_name: str | None = None) -> ResolvedTemplate:
    """Resolve a workspace template by name from the Registry."""
    from agentworks.resources.access import kind_dict

    return resolve_from_dict(kind_dict(registry, "workspace-template"), template_name)


def _resolve(
    templates: dict[str, WorkspaceTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
) -> ResolvedTemplate:
    """Depth-first, left-to-right resolution of a template.

    ``_visiting`` carries the chain of in-progress resolves so cycles
    raise ``ConfigError`` instead of ``RecursionError``. The framework's
    cycle pass at build_registry time is the canonical check; this guard
    is the safety net for callers that resolve without going through
    build_registry (Phase 2a.2).
    """
    if name in _visiting:
        path = " -> ".join((*_visiting, name))
        raise ConfigError(
            f"workspace_templates inheritance cycle detected: {path}"
        )

    if name not in templates:
        return ResolvedTemplate(name=name)

    tmpl = templates[name]
    result = ResolvedTemplate(name=name)
    next_visiting = (*_visiting, name)

    # Walk parents first
    for parent_name in tmpl.inherits:
        parent = _resolve(templates, parent_name, next_visiting)
        _merge(result, parent)

    # Apply this template's own values (last-one-wins)
    _merge_template(result, tmpl)
    result.name = name  # always use the originally requested name
    return result


def _merge(target: ResolvedTemplate, source: ResolvedTemplate) -> None:
    """Merge source into target (source wins for scalars)."""
    if source.repo is not None:
        target.repo = source.repo
    target.tmuxinator = source.tmuxinator
    target.env = {**target.env, **source.env}


def _merge_template(target: ResolvedTemplate, tmpl: WorkspaceTemplate) -> None:
    """Merge a raw WorkspaceTemplate into a ResolvedTemplate."""
    if tmpl.repo is not None:
        target.repo = tmpl.repo
    if tmpl.tmuxinator is not None:
        target.tmuxinator = tmpl.tmuxinator
    if tmpl.env:
        target.env = {**target.env, **tmpl.env}
