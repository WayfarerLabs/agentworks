"""Agent template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in default template fallback. Follows the same pattern as VM and
workspace templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentworks.config import AgentTemplate, Config


@dataclass
class ResolvedAgentTemplate:
    """A fully resolved agent template with all inheritance applied."""

    name: str
    shell: str = "bash"
    git_credentials: list[str] = field(default_factory=list)
    user_install_commands: list[str] = field(default_factory=list)
    dotfiles_source: str | None = None
    dotfiles_destination: str = "~/.dotfiles"
    dotfiles_install_cmd: str = "./install.sh"
    mise_activate: bool = True
    mise_packages: list[str] = field(default_factory=list)
    mise_lockfile: str | None = None
    mise_allow_unlocked: bool = False
    mise_install_before: str = "7d"
    mise_prune_on_reinit: bool = True


def resolve_from_dict(
    templates: dict[str, AgentTemplate],
    template_name: str | None = None,
) -> ResolvedAgentTemplate:
    """Resolve an agent template from a templates dict (no Config required)."""
    if template_name is not None and template_name != "default":
        if template_name not in templates:
            msg = f"Unknown agent template: {template_name}"
            raise ValueError(msg)
        return _resolve(templates, template_name)

    if "default" in templates:
        return _resolve(templates, "default")

    return ResolvedAgentTemplate(name="default")


def resolve_template(config: Config, template_name: str | None = None) -> ResolvedAgentTemplate:
    """Resolve an agent template by name, applying inheritance."""
    return resolve_from_dict(config.agent_templates, template_name)


def _resolve(templates: dict[str, AgentTemplate], name: str) -> ResolvedAgentTemplate:
    """Depth-first, left-to-right resolution."""
    if name not in templates:
        return ResolvedAgentTemplate(name=name)

    tmpl = templates[name]
    result = ResolvedAgentTemplate(name=name)

    for parent_name in tmpl.inherits:
        parent = _resolve(templates, parent_name)
        _merge(result, parent)

    _merge_template(result, tmpl)
    result.name = name
    return result


def _append_dedupe(target: list[str], source: list[str]) -> list[str]:
    """Append source items to target, skipping dupes. Preserves order."""
    seen = set(target)
    result = list(target)
    for item in source:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _merge_map(target: dict[str, str], source: dict[str, str]) -> dict[str, str]:
    """Merge source map into target. Source wins on key collision."""
    return {**target, **source}


def _merge(target: ResolvedAgentTemplate, source: ResolvedAgentTemplate) -> None:
    """Merge source into target. Scalars: source wins. Lists: append with dedupe."""
    target.shell = source.shell
    target.git_credentials = _append_dedupe(target.git_credentials, source.git_credentials)
    target.user_install_commands = _append_dedupe(target.user_install_commands, source.user_install_commands)
    target.dotfiles_source = source.dotfiles_source
    target.dotfiles_destination = source.dotfiles_destination
    target.dotfiles_install_cmd = source.dotfiles_install_cmd
    target.mise_activate = source.mise_activate
    target.mise_packages = _append_dedupe(target.mise_packages, source.mise_packages)
    target.mise_lockfile = source.mise_lockfile
    target.mise_allow_unlocked = source.mise_allow_unlocked
    target.mise_install_before = source.mise_install_before
    target.mise_prune_on_reinit = source.mise_prune_on_reinit


def _merge_template(target: ResolvedAgentTemplate, tmpl: AgentTemplate) -> None:
    """Merge a raw AgentTemplate into a ResolvedAgentTemplate. None = not set, skip.
    Scalars: child overrides. Lists: append with dedupe."""
    if tmpl.shell is not None:
        target.shell = tmpl.shell
    if tmpl.git_credentials is not None:
        target.git_credentials = _append_dedupe(target.git_credentials, tmpl.git_credentials)
    if tmpl.user_install_commands is not None:
        target.user_install_commands = _append_dedupe(target.user_install_commands, tmpl.user_install_commands)
    if tmpl.dotfiles_source is not None:
        target.dotfiles_source = tmpl.dotfiles_source
    if tmpl.dotfiles_destination is not None:
        target.dotfiles_destination = tmpl.dotfiles_destination
    if tmpl.dotfiles_install_cmd is not None:
        target.dotfiles_install_cmd = tmpl.dotfiles_install_cmd
    if tmpl.mise_activate is not None:
        target.mise_activate = tmpl.mise_activate
    if tmpl.mise_packages is not None:
        target.mise_packages = _append_dedupe(target.mise_packages, tmpl.mise_packages)
    if tmpl.mise_lockfile is not None:
        target.mise_lockfile = tmpl.mise_lockfile
    if tmpl.mise_allow_unlocked is not None:
        target.mise_allow_unlocked = tmpl.mise_allow_unlocked
    if tmpl.mise_install_before is not None:
        target.mise_install_before = tmpl.mise_install_before
    if tmpl.mise_prune_on_reinit is not None:
        target.mise_prune_on_reinit = tmpl.mise_prune_on_reinit
