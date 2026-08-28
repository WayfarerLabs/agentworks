"""VM template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in default template fallback. Follows the same pattern as workspace
templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import unknown_template_error

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.db import Database
    from agentworks.env import EnvEntry
    from agentworks.resources.inheritance import LayerContribution, LayeredResolution
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


def resolve_from_dict(
    templates: dict[str, VMTemplate],
    template_name: str | None = None,
    *,
    overlay: VMTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedVMTemplate:
    """Resolve a VM template from a templates dict (no Config required).

    Used during config loading to resolve the default template eagerly.
    """
    return resolve_from_dict_with_provenance(
        templates,
        template_name,
        overlay=overlay,
        instance_name=instance_name,
    ).value


def resolve_from_dict_with_provenance(
    templates: dict[str, VMTemplate],
    template_name: str | None = None,
    *,
    overlay: VMTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedVMTemplate]:
    """Resolve VM declarations while retaining per-value layer provenance."""
    if template_name is not None and template_name != "default":
        if template_name not in templates:
            raise unknown_template_error(
                kind="vm-template",
                label="VM template",
                name=template_name,
                available=templates,
            )
        return _resolve_with_provenance(templates, template_name, overlay=overlay, instance_name=instance_name)

    if "default" in templates:
        return _resolve_with_provenance(templates, "default", overlay=overlay, instance_name=instance_name)

    return _resolve_with_provenance({}, "default", overlay=overlay, instance_name=instance_name)


def _resolve_from_dict(
    templates: Mapping[str, VMTemplate],
    name: str,
    *,
    overlay: VMTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedVMTemplate:
    """Resolve ``name``'s chain, defaults applied.

    ONE accumulator, folded over the chain's declarations. That is what
    keeps a silent parent silent: every write goes through
    :func:`_merge_template`, which skips an undeclared field, so there is
    never a second defaults-applied template for a later parent to
    overwrite an earlier one's real value with. Resolving each parent to
    its own ``ResolvedVMTemplate`` first would destroy that distinction
    before the merge could read it, since a resolved template cannot say
    which of its values it was given and which it defaulted to.

    A name with no row resolves to the built-in defaults, via an empty
    chain.
    """
    return _resolve_with_provenance(
        templates,
        name,
        overlay=overlay,
        instance_name=instance_name,
    ).value


def _resolve_with_provenance(
    templates: Mapping[str, VMTemplate],
    name: str,
    *,
    overlay: VMTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedVMTemplate]:
    # Imported here, not at module level: ``agentworks.resources``'s package
    # init loads every kind module, and every kind module reaches this one.
    from agentworks.resources.inheritance import (
        DeclarationLayer,
        LayerSource,
        LayerSourceKind,
        resolution_layers,
        run_layer_fold,
    )

    layers = [
        DeclarationLayer(
            LayerSource(LayerSourceKind.TEMPLATE, "vm-template", layer.name),
            layer,
        )
        for layer in resolution_layers(templates, name, "vm-template")
    ]
    if overlay is not None:
        layers.append(
            DeclarationLayer(
                LayerSource(LayerSourceKind.INSTANCE, "vm", instance_name or overlay.name),
                overlay,
            )
        )
    return run_layer_fold(
        ResolvedVMTemplate(name=name),
        layers,
        _merge_template,
        default_paths=((field,) for field in ("cpus", "memory", "disk", "swap", "tailscale_auth_key")),
        default_resource_kind="vm-template",
        default_name=name,
    )


def effective_template(templates: Mapping[str, VMTemplate], name: str) -> ResolvedVMTemplate:
    """The effective (merged) declaration of ``name``, for the finalize
    passes.

    Distinct from :func:`resolve_from_dict` in exactly one way, which is
    the reason it exists: it is TOTAL. ``dependencies`` must never raise
    (the graph is built before anything is validated), and the only thing
    the merge can raise on is a cyclic chain, which has no effective
    declaration to compute. Degrading to the kind's defaults is safe
    because the value is provably never observed: a degraded row implies a
    loop among present nodes, and finalize's cycle pass raises on it
    before the graph is built, let alone read.
    """
    return effective_template_with_provenance(templates, name).value


def effective_template_with_provenance(
    templates: Mapping[str, VMTemplate],
    name: str,
) -> LayeredResolution[ResolvedVMTemplate]:
    """The total finalize view with the provenance of every surviving value."""
    from agentworks.errors import InheritanceCycleError
    from agentworks.resources.inheritance import LayeredResolution

    try:
        return _resolve_with_provenance(templates, name)
    except InheritanceCycleError:
        return LayeredResolution(ResolvedVMTemplate(name=name), {})


def resolve_template(
    registry: Registry,
    template_name: str | None = None,
    *,
    overlay: VMTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedVMTemplate:
    """Resolve a VM template by name, applying inheritance.

    Selection order:
    1. Explicit template_name
    2. "default" template if it exists
    3. Built-in default template
    """

    return resolve_template_with_provenance(
        registry,
        template_name,
        overlay=overlay,
        instance_name=instance_name,
    ).value


def resolve_template_with_provenance(
    registry: Registry,
    template_name: str | None = None,
    *,
    overlay: VMTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedVMTemplate]:
    from agentworks.resources.access import kind_dict

    return resolve_from_dict_with_provenance(
        kind_dict(registry, "vm-template"),
        template_name,
        overlay=overlay,
        instance_name=instance_name,
    )


def resolve_live_template(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> ResolvedVMTemplate:
    """Resolve a persisted VM's template chain plus its stored final layer."""
    from typing import cast

    from agentworks.instance_specs import get_instance_overlay

    overlay = get_instance_overlay(db, "vm", instance_name)
    if overlay is None:
        return resolve_template(registry, template_name)
    return resolve_template(
        registry,
        template_name,
        overlay=cast("VMTemplate", overlay.declaration),
        instance_name=instance_name,
    )


def resolve_live_template_with_provenance(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> LayeredResolution[ResolvedVMTemplate]:
    """Resolve a persisted VM and retain its template/instance provenance."""
    from typing import cast

    from agentworks.instance_specs import get_instance_overlay

    overlay = get_instance_overlay(db, "vm", instance_name)
    return resolve_template_with_provenance(
        registry,
        template_name,
        overlay=None if overlay is None else cast("VMTemplate", overlay.declaration),
        instance_name=instance_name,
    )


def _append_dedupe(target: list[str], source: list[str]) -> list[str]:
    """Append source items to target, skipping dupes. Preserves order."""
    seen = set(target)
    result = list(target)
    for item in source:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _merge_template(
    target: ResolvedVMTemplate,
    tmpl: VMTemplate,
    _source: object,
) -> tuple[ResolvedVMTemplate, tuple[LayerContribution, ...]]:
    """Fold one declared VMTemplate into the accumulator. None = not set,
    skip. Scalars: later layer overrides. Lists: append with dedupe.

    The ONLY writer of a ``ResolvedVMTemplate``'s fields, which is what
    makes "a layer that declares nothing changes nothing" hold by
    construction rather than by two parallel field lists agreeing about
    every field forever.
    """
    from agentworks.resources.inheritance import LayerContribution

    touched: list[LayerContribution] = []
    if tmpl.cpus is not None:
        target.cpus = tmpl.cpus
        touched.append(LayerContribution.replacement("cpus"))
    if tmpl.memory is not None:
        target.memory = tmpl.memory
        touched.append(LayerContribution.replacement("memory"))
    if tmpl.disk is not None:
        target.disk = tmpl.disk
        touched.append(LayerContribution.replacement("disk"))
    if tmpl.swap is not None:
        target.swap = tmpl.swap
        touched.append(LayerContribution.replacement("swap"))
    if tmpl.apt is not None:
        target.apt = _append_dedupe(target.apt, tmpl.apt)
        touched.extend(LayerContribution.contribution("apt", item) for item in tmpl.apt)
    if tmpl.apt_packages is not None:
        target.apt_packages = _append_dedupe(target.apt_packages, tmpl.apt_packages)
        touched.extend(LayerContribution.contribution("apt_packages", item) for item in tmpl.apt_packages)
    if tmpl.snap is not None:
        target.snap = _append_dedupe(target.snap, tmpl.snap)
        touched.extend(LayerContribution.contribution("snap", item) for item in tmpl.snap)
    if tmpl.system_install_commands is not None:
        target.system_install_commands = _append_dedupe(target.system_install_commands, tmpl.system_install_commands)
        touched.extend(
            LayerContribution.contribution("system_install_commands", item) for item in tmpl.system_install_commands
        )
    if tmpl.env:
        target.env = {**target.env, **tmpl.env}
        touched.extend(LayerContribution.replacement("env", key) for key in tmpl.env)
    if tmpl.tailscale_auth_key is not None:
        target.tailscale_auth_key = tmpl.tailscale_auth_key
        touched.append(LayerContribution.replacement("tailscale_auth_key"))
    return target, tuple(touched)
