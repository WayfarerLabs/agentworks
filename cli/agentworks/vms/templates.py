"""VM template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in default template fallback. Follows the same pattern as workspace
templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import inheritance_cycle_error, unknown_template_error

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.env import EnvEntry
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
) -> ResolvedVMTemplate:
    """Resolve a VM template from a templates dict (no Config required).

    Used during config loading to resolve the default template eagerly.
    """
    if template_name is not None and template_name != "default":
        if template_name not in templates:
            raise unknown_template_error(
                kind="vm-template",
                label="VM template",
                name=template_name,
                available=templates,
            )
        return _resolve_from_dict(templates, template_name)

    if "default" in templates:
        return _resolve_from_dict(templates, "default")

    return ResolvedVMTemplate(name="default")


def _layers(
    templates: Mapping[str, VMTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
) -> list[VMTemplate]:
    """The DECLARATIONS ``name`` merges from, in merge order: each
    parent's own chain first (left to right), then the row itself.

    The same chain, and deliberately the same order, as
    :func:`agentworks.resources.inheritance.merge_layers`, because a
    provenance answer read off those layers has to name the layer whose
    value this resolver's fold actually kept. It differs in exactly one
    way: a cycle RAISES here. A resolve has a caller that can be told,
    whereas ``merge_layers`` runs inside the finalize build walk, which
    may not raise.

    A name with no row contributes NO layer, matching ``merge_layers``:
    an unresolved parent is the miss policy's to report, and a template
    that stood in for it would be a fabricated declaration whose every
    field says "the built-in default".

    ``_visiting`` carries the chain of in-progress walks so a cycle
    raises a clean ``InheritanceCycleError`` (matching the framework's
    cycle-pass error shape) instead of crashing with ``RecursionError``.
    This is the resolver's internal safety net; the canonical cycle check
    lives in ``Registry.finalize`` at build_registry time, but this
    resolver is also called eagerly by ``load_config`` before any registry
    is built, so it needs its own guard, and :func:`effective_template`
    keys on the type to stay total.
    """
    if name in _visiting:
        raise inheritance_cycle_error("vm-template", (*_visiting, name))

    if name not in templates:
        return []

    tmpl = templates[name]
    next_visiting = (*_visiting, name)
    layers = [layer for parent in tmpl.inherits for layer in _layers(templates, parent, next_visiting)]
    layers.append(tmpl)
    return layers


def _resolve_from_dict(
    templates: Mapping[str, VMTemplate],
    name: str,
    _visiting: tuple[str, ...] = (),
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
    result = ResolvedVMTemplate(name=name)
    for layer in _layers(templates, name, _visiting):
        _merge_template(result, layer)
    return result


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
    from agentworks.errors import InheritanceCycleError

    try:
        return _resolve_from_dict(templates, name)
    except InheritanceCycleError:
        return ResolvedVMTemplate(name=name)


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


def _merge_template(target: ResolvedVMTemplate, tmpl: VMTemplate) -> None:
    """Fold one declared VMTemplate into the accumulator. None = not set,
    skip. Scalars: later layer overrides. Lists: append with dedupe.

    The ONLY writer of a ``ResolvedVMTemplate``'s fields, which is what
    makes "a layer that declares nothing changes nothing" hold by
    construction rather than by two parallel field lists agreeing about
    every field forever.
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
