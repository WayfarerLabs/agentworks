"""Session template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in default template fallback. Follows the same pattern as VM,
workspace, and agent templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.config import Config, SessionTemplate
    from agentworks.env import EnvEntry


@dataclass
class ResolvedSessionTemplate:
    """A fully resolved session template with all inheritance applied."""

    name: str
    command: str = ""
    description: str = "Login shell"
    restart_command: str = ""
    required_commands: list[str] = field(default_factory=list)
    env: dict[str, EnvEntry] = field(default_factory=dict)


def _append_dedupe(target: list[str], source: list[str]) -> list[str]:
    """Append source items to target, skipping dupes. Preserves order."""
    seen = set(target)
    result = list(target)
    for item in source:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


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
            msg = f"Unknown session template: {template_name}"
            raise ValueError(msg)
        return _resolve(templates, template_name)

    if "default" in templates:
        return _resolve(templates, "default")

    return ResolvedSessionTemplate(name="default")


def resolve_template(config: Config, template_name: str | None = None) -> ResolvedSessionTemplate:
    """Resolve a session template by name, applying inheritance."""
    return resolve_from_dict(config.session_templates, template_name)


def _resolve(templates: dict[str, SessionTemplate], name: str) -> ResolvedSessionTemplate:
    """Depth-first, left-to-right resolution."""
    if name not in templates:
        return ResolvedSessionTemplate(name=name)

    tmpl = templates[name]
    result = ResolvedSessionTemplate(name=name)

    for parent_name in tmpl.inherits:
        parent = _resolve(templates, parent_name)
        _merge(result, parent)

    _merge_template(result, tmpl)
    result.name = name
    return result


def _merge(target: ResolvedSessionTemplate, source: ResolvedSessionTemplate) -> None:
    """Merge source into target. Scalars: source wins. Maps: merge with source
    wins. Lists (required_commands): unioned, preserving order."""
    target.command = source.command
    target.description = source.description
    target.restart_command = source.restart_command
    target.required_commands = _append_dedupe(target.required_commands, source.required_commands)
    target.env = _merge_map(target.env, source.env)


def _merge_template(target: ResolvedSessionTemplate, tmpl: SessionTemplate) -> None:
    """Merge a raw SessionTemplate into a ResolvedSessionTemplate. None = not set, skip.
    Scalars: child overrides. Maps: merge with child wins. Lists
    (required_commands): unioned, preserving order."""
    if tmpl.command is not None:
        target.command = tmpl.command
    if tmpl.description is not None:
        target.description = tmpl.description
    if tmpl.restart_command is not None:
        target.restart_command = tmpl.restart_command
    if tmpl.required_commands is not None:
        target.required_commands = _append_dedupe(target.required_commands, tmpl.required_commands)
    if tmpl.env is not None:
        target.env = _merge_map(target.env, tmpl.env)
