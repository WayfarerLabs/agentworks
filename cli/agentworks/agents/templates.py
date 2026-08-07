"""Agent template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in default template fallback. Follows the same pattern as VM and
workspace templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import inheritance_cycle_error, unknown_template_error

if TYPE_CHECKING:
    from collections.abc import Mapping

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


def effective_template(templates: Mapping[str, AgentTemplate], name: str) -> ResolvedAgentTemplate:
    """The effective (merged) declaration of ``name``, for the finalize
    passes. TOTAL; see ``vms/templates.effective_template`` for why the
    degradation on a cyclic chain is never observed."""
    from agentworks.errors import InheritanceCycleError

    try:
        return _resolve(templates, name)
    except InheritanceCycleError:
        return ResolvedAgentTemplate(name=name)


def _layers(
    templates: Mapping[str, AgentTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
) -> list[AgentTemplate]:
    """The DECLARATIONS ``name`` merges from, in merge order: each
    parent's own chain first (left to right), then the row itself. See
    ``vms.templates._layers`` for the shape all four resolvers share and
    why it matches ``resources.inheritance.merge_layers``.

    ``_visiting`` carries the chain of in-progress walks so cycles raise
    a clean ``InheritanceCycleError`` instead of crashing with
    ``RecursionError``. The framework's ``Registry.finalize`` cycle pass
    is the canonical check at build_registry time; this resolver-internal
    guard is the safety net for the load-time eager-resolve path (Phase
    2a.2; mirrors the vm_template resolver guard), and
    :func:`effective_template` keys on the type to stay total.
    """
    if name in _visiting:
        raise inheritance_cycle_error("agent-template", (*_visiting, name))

    if name not in templates:
        return []

    tmpl = templates[name]
    next_visiting = (*_visiting, name)
    layers = [layer for parent in tmpl.inherits for layer in _layers(templates, parent, next_visiting)]
    layers.append(tmpl)
    return layers


def _resolve(
    templates: Mapping[str, AgentTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
) -> ResolvedAgentTemplate:
    """Resolve ``name``'s chain, defaults applied: one accumulator folded
    over the chain's declarations. See ``vms.templates._resolve_from_dict``
    for why the fold reads the DECLARATIONS rather than each parent's
    resolved template.
    """
    result = ResolvedAgentTemplate(name=name)
    for layer in _layers(templates, name, _visiting):
        _merge_template(result, layer)
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


def _merge_template(target: ResolvedAgentTemplate, tmpl: AgentTemplate) -> None:
    """Fold one declared AgentTemplate into the accumulator. None = not
    set, skip. Scalars: later layer overrides. Lists: append with dedupe.
    The only writer of a ``ResolvedAgentTemplate``'s fields."""
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
