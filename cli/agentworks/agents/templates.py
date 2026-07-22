"""Agent template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in default template fallback. Follows the same pattern as VM and
workspace templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError, unknown_template_error

if TYPE_CHECKING:
    from agentworks.agents.template import AgentTemplate
    from agentworks.env import EnvEntry
    from agentworks.resources.registry import Registry


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
    claude_marketplaces: list[str] = field(default_factory=list)
    claude_plugins: list[str] = field(default_factory=list)
    env: dict[str, EnvEntry] = field(default_factory=dict)


def resolve_from_dict(
    templates: dict[str, AgentTemplate],
    template_name: str | None = None,
) -> ResolvedAgentTemplate:
    """Resolve an agent template from a templates dict (no Config required)."""
    if template_name is not None and template_name != "default":
        if template_name not in templates:
            raise unknown_template_error(
                kind="agent-template",
                label="agent template",
                name=template_name,
                available=templates,
            )
        return _resolve(templates, template_name)

    if "default" in templates:
        return _resolve(templates, "default")

    return ResolvedAgentTemplate(name="default")


def resolve_template(registry: Registry, template_name: str | None = None) -> ResolvedAgentTemplate:
    """Resolve an agent template by name, applying inheritance."""
    from agentworks.resources.access import kind_dict

    return resolve_from_dict(kind_dict(registry, "agent-template"), template_name)


def _resolve(
    templates: dict[str, AgentTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
) -> ResolvedAgentTemplate:
    """Depth-first, left-to-right resolution.

    ``_visiting`` carries the chain of in-progress resolves so cycles
    raise a clean ``ConfigError`` instead of crashing with
    ``RecursionError``. The framework's ``Registry.finalize`` cycle pass
    is the canonical check at build_registry time; this resolver-internal
    guard is the safety net for the load-time eager-resolve path (Phase
    2a.2; mirrors the vm_template resolver guard).
    """
    if name in _visiting:
        path = " -> ".join((*_visiting, name))
        raise ConfigError(f"agent_templates inheritance cycle detected: {path}")

    if name not in templates:
        return ResolvedAgentTemplate(name=name)

    tmpl = templates[name]
    result = ResolvedAgentTemplate(name=name)
    next_visiting = (*_visiting, name)

    for parent_name in tmpl.inherits:
        parent = _resolve(templates, parent_name, next_visiting)
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
    target.claude_marketplaces = _append_dedupe(target.claude_marketplaces, source.claude_marketplaces)
    target.claude_plugins = _append_dedupe(target.claude_plugins, source.claude_plugins)
    target.env = {**target.env, **source.env}


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
    if tmpl.claude_marketplaces is not None:
        target.claude_marketplaces = _append_dedupe(target.claude_marketplaces, tmpl.claude_marketplaces)
    if tmpl.claude_plugins is not None:
        target.claude_plugins = _append_dedupe(target.claude_plugins, tmpl.claude_plugins)
    if tmpl.env:
        target.env = {**target.env, **tmpl.env}
