"""VM template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in default template fallback. Follows the same pattern as workspace
templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from agentworks.env import EnvEntry
    from agentworks.resources.reference import ResourceReference
    from agentworks.resources.registry import Registry
    from agentworks.vms.template import VMTemplate


@dataclass
class ResolvedVMTemplate:
    """A fully resolved VM template with all inheritance applied."""

    name: str
    # Provisioning. No site field: placement is host/operator-scoped
    # (--site / defaults.site / the infer-prompt model), never template
    # state.
    cpus: int = 4
    memory: int = 8
    disk: int = 50
    swap: int = 4
    # System-wide init
    apt: list[str] = field(default_factory=list)
    apt_packages: list[str] = field(default_factory=list)
    snap: list[str] = field(default_factory=list)
    system_install_commands: list[str] = field(default_factory=list)
    # Env (declared per-template; merged child-overrides-parent)
    env: dict[str, EnvEntry] = field(default_factory=dict)
    # Secret name for the Tailscale auth key (default ``"tailscale-auth-key"``).
    # Inheritance applies like other scalar fields: child overrides parent.
    tailscale_auth_key: str = "tailscale-auth-key"

    def referenced_resources(self) -> list[ResourceReference]:
        """Emit the resolved template's references: env-block secret
        refs (with inheritance applied via the merged ``env`` dict) plus
        the Tailscale auth-key secret. Used by ``vm create`` / ``vm     reinit``
        for the eager-resolve subgraph walk.
        """
        from agentworks.env.entry import env_references
        from agentworks.vms.template import tailscale_secret_reference

        refs: list[ResourceReference] = list(
            env_references(self.env, ("vm-template", self.name))
        )
        refs.append(tailscale_secret_reference(self.tailscale_auth_key, self.name))
        return refs


def resolve_from_dict(
    templates: dict[str, VMTemplate],
    template_name: str | None = None,
) -> ResolvedVMTemplate:
    """Resolve a VM template from a templates dict (no Config required).

    Used during config loading to resolve the default template eagerly.
    """
    if template_name is not None and template_name != "default":
        if template_name not in templates:
            msg = f"Unknown VM template: {template_name}"
            raise ValueError(msg)
        return _resolve_from_dict(templates, template_name)

    if "default" in templates:
        return _resolve_from_dict(templates, "default")

    return ResolvedVMTemplate(name="default")


def _resolve_from_dict(
    templates: dict[str, VMTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
) -> ResolvedVMTemplate:
    """Depth-first resolution using a templates dict.

    ``_visiting`` carries the chain of in-progress resolves so cycles
    raise a clean ``ConfigError`` (matching the framework's cycle-pass
    error shape) instead of crashing with ``RecursionError``. This is
    the resolver's internal safety net -- the canonical cycle check
    lives in ``Registry.finalize`` at build_registry time, but this
    resolver is also called eagerly by ``load_config`` before any
    registry is built (FRD R6 / Phase 2a.1), so it needs its own
    guard.
    """
    if name in _visiting:
        path = " -> ".join((*_visiting, name))
        raise ConfigError(
            f"vm_templates inheritance cycle detected: {path}"
        )

    if name not in templates:
        # Implicit default: return built-in defaults
        return ResolvedVMTemplate(name=name)

    tmpl = templates[name]
    result = ResolvedVMTemplate(name=name)
    next_visiting = (*_visiting, name)

    for parent_name in tmpl.inherits:
        parent = _resolve_from_dict(templates, parent_name, next_visiting)
        _merge(result, parent)

    _merge_template(result, tmpl)
    result.name = name
    return result


def resolve_template(registry: Registry, template_name: str | None = None) -> ResolvedVMTemplate:
    """Resolve a VM template by name, applying inheritance.

    Selection order:
    1. Explicit template_name
    2. "default" template if it exists
    3. Built-in default template
    """
    from agentworks.resources.access import kind_dict

    return resolve_from_dict(kind_dict(registry, "vm-template"), template_name)


def _append_dedupe(target: list[str], source: list[str]) -> list[str]:
    """Append source items to target, skipping dupes. Preserves order."""
    seen = set(target)
    result = list(target)
    for item in source:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _merge(target: ResolvedVMTemplate, source: ResolvedVMTemplate) -> None:
    """Merge source into target. Scalars: source wins. Lists: append with dedupe."""
    target.cpus = source.cpus
    target.memory = source.memory
    target.disk = source.disk
    target.swap = source.swap
    target.apt = _append_dedupe(target.apt, source.apt)
    target.apt_packages = _append_dedupe(target.apt_packages, source.apt_packages)
    target.snap = _append_dedupe(target.snap, source.snap)
    target.system_install_commands = _append_dedupe(target.system_install_commands, source.system_install_commands)
    target.env = {**target.env, **source.env}
    target.tailscale_auth_key = source.tailscale_auth_key


def _merge_template(target: ResolvedVMTemplate, tmpl: VMTemplate) -> None:
    """Merge a raw VMTemplate into a ResolvedVMTemplate. None = not set, skip.
    Scalars: child overrides. Lists: append with dedupe.
    """
    if tmpl.cpus is not None:
        target.cpus = tmpl.cpus
    if tmpl.memory is not None:
        target.memory = tmpl.memory
    if tmpl.disk is not None:
        target.disk = tmpl.disk
    if tmpl.swap is not None:
        target.swap = tmpl.swap
    if tmpl.apt is not None:
        target.apt = _append_dedupe(target.apt, tmpl.apt)
    if tmpl.apt_packages is not None:
        target.apt_packages = _append_dedupe(target.apt_packages, tmpl.apt_packages)
    if tmpl.snap is not None:
        target.snap = _append_dedupe(target.snap, tmpl.snap)
    if tmpl.system_install_commands is not None:
        target.system_install_commands = _append_dedupe(target.system_install_commands, tmpl.system_install_commands)
    if tmpl.env:
        target.env = {**target.env, **tmpl.env}
    if tmpl.tailscale_auth_key is not None:
        target.tailscale_auth_key = tmpl.tailscale_auth_key
