"""VM template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in default template fallback. Follows the same pattern as workspace
templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config, VMTemplate


@dataclass
class ResolvedVMTemplate:
    """A fully resolved VM template with all inheritance applied."""

    name: str
    # Provisioning
    cpus: int = 4
    memory: int = 8
    disk: int = 50
    azure_vm_size: str = "Standard_B2s"
    swap: int = 4
    # System-wide init
    apt: list[str] = field(default_factory=list)
    apt_packages: list[str] = field(default_factory=list)
    snap: list[str] = field(default_factory=list)
    system_install_commands: list[str] = field(default_factory=list)
    install_mise: bool = True
    # Nerf tools
    install_nerf_tools: bool = False
    skip_nerf_defaults: bool = False
    nerf_addl_manifests: list[Path] = field(default_factory=list)
    nerf_keep_existing: bool = False
    nerf_bin_dir: str = "/opt/agentworks/nerf/bin"
    nerf_skills_dir: str = "/opt/agentworks/nerf/skills"


def resolve_from_dict(
    templates: dict[str, VMTemplate], template_name: str | None = None,
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


def _resolve_from_dict(templates: dict[str, VMTemplate], name: str) -> ResolvedVMTemplate:
    """Depth-first resolution using a templates dict."""
    if name not in templates:
        # Implicit default: return built-in defaults
        return ResolvedVMTemplate(name=name)

    tmpl = templates[name]
    result = ResolvedVMTemplate(name=name)

    for parent_name in tmpl.inherits:
        parent = _resolve_from_dict(templates, parent_name)
        _merge(result, parent)

    _merge_template(result, tmpl)
    result.name = name
    return result


def resolve_template(config: Config, template_name: str | None = None) -> ResolvedVMTemplate:
    """Resolve a VM template by name, applying inheritance.

    Selection order:
    1. Explicit template_name
    2. "default" template if it exists
    3. Built-in default template
    """
    return resolve_from_dict(config.vm_templates, template_name)


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


def _merge(target: ResolvedVMTemplate, source: ResolvedVMTemplate) -> None:
    """Merge source into target. Scalars: source wins. Lists: append with dedupe."""
    target.cpus = source.cpus
    target.memory = source.memory
    target.disk = source.disk
    target.azure_vm_size = source.azure_vm_size
    target.swap = source.swap
    target.apt = _append_dedupe(target.apt, source.apt)
    target.apt_packages = _append_dedupe(target.apt_packages, source.apt_packages)
    target.snap = _append_dedupe(target.snap, source.snap)
    target.system_install_commands = _append_dedupe(target.system_install_commands, source.system_install_commands)
    target.install_mise = source.install_mise
    target.install_nerf_tools = source.install_nerf_tools
    target.skip_nerf_defaults = source.skip_nerf_defaults
    target.nerf_addl_manifests = list(source.nerf_addl_manifests)
    target.nerf_keep_existing = source.nerf_keep_existing
    target.nerf_bin_dir = source.nerf_bin_dir
    target.nerf_skills_dir = source.nerf_skills_dir


def _merge_template(target: ResolvedVMTemplate, tmpl: VMTemplate) -> None:
    """Merge a raw VMTemplate into a ResolvedVMTemplate. None = not set, skip.
    Scalars: child overrides. Lists: append with dedupe."""
    if tmpl.cpus is not None:
        target.cpus = tmpl.cpus
    if tmpl.memory is not None:
        target.memory = tmpl.memory
    if tmpl.disk is not None:
        target.disk = tmpl.disk
    if tmpl.azure_vm_size is not None:
        target.azure_vm_size = tmpl.azure_vm_size
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
    if tmpl.install_mise is not None:
        target.install_mise = tmpl.install_mise
    if tmpl.install_nerf_tools is not None:
        target.install_nerf_tools = tmpl.install_nerf_tools
    if tmpl.skip_nerf_defaults is not None:
        target.skip_nerf_defaults = tmpl.skip_nerf_defaults
    if tmpl.nerf_addl_manifests is not None:
        target.nerf_addl_manifests = list(tmpl.nerf_addl_manifests)
    if tmpl.nerf_keep_existing is not None:
        target.nerf_keep_existing = tmpl.nerf_keep_existing
    if tmpl.nerf_bin_dir is not None:
        target.nerf_bin_dir = tmpl.nerf_bin_dir
    if tmpl.nerf_skills_dir is not None:
        target.nerf_skills_dir = tmpl.nerf_skills_dir
