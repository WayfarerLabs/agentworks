"""Workspace template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in empty template fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.config import Config, WorkspaceTemplate
    from agentworks.env import EnvEntry


@dataclass
class ResolvedTemplate:
    """A fully resolved workspace template with all inheritance applied."""

    name: str
    repo: str | None = None
    tmuxinator: bool = True
    env: dict[str, EnvEntry] = field(default_factory=dict)


def resolve_template(config: Config, template_name: str | None = None) -> ResolvedTemplate:
    """Resolve a workspace template by name, applying inheritance.

    Selection order:
    1. Explicit template_name
    2. "default" template if it exists
    3. Built-in empty template (tmuxinator=True, no repo)
    """
    if template_name is not None and template_name != "default":
        if template_name not in config.workspace_templates:
            msg = f"Unknown workspace template: {template_name}"
            raise ValueError(msg)
        return _resolve(config, template_name)

    if "default" in config.workspace_templates:
        return _resolve(config, "default")

    return ResolvedTemplate(name="default")


def _resolve(config: Config, name: str) -> ResolvedTemplate:
    """Depth-first, left-to-right resolution of a template."""
    if name not in config.workspace_templates:
        return ResolvedTemplate(name=name)

    tmpl = config.workspace_templates[name]
    result = ResolvedTemplate(name=name)

    # Walk parents first
    for parent_name in tmpl.inherits:
        parent = _resolve(config, parent_name)
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
