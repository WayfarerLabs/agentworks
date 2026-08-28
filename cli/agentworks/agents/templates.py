"""Agent template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in default template fallback. Follows the same pattern as VM and
workspace templates.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, cast

from agentworks.errors import unknown_template_error

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.agents.template import AgentTemplate
    from agentworks.db import Database
    from agentworks.env.entry import EnvEntry
    from agentworks.resources.inheritance import LayerContribution, LayeredResolution
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
    *,
    overlay: AgentTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedAgentTemplate:
    """Resolve an agent template from a templates dict (no Config required)."""
    return resolve_from_dict_with_provenance(
        templates,
        template_name,
        overlay=overlay,
        instance_name=instance_name,
    ).value


def resolve_from_dict_with_provenance(
    templates: dict[str, AgentTemplate],
    template_name: str | None = None,
    *,
    overlay: AgentTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedAgentTemplate]:
    if template_name is not None and template_name != "default":
        if template_name not in templates:
            raise unknown_template_error(
                kind="agent-template",
                label="agent template",
                name=template_name,
                available=templates,
            )
        return _resolve_with_provenance(templates, template_name, overlay=overlay, instance_name=instance_name)

    if "default" in templates:
        return _resolve_with_provenance(templates, "default", overlay=overlay, instance_name=instance_name)

    return _resolve_with_provenance({}, "default", overlay=overlay, instance_name=instance_name)


def resolve_template(
    registry: Registry,
    template_name: str | None = None,
    *,
    overlay: AgentTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedAgentTemplate:
    """Resolve an agent template by name, applying inheritance."""

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
    overlay: AgentTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedAgentTemplate]:
    from agentworks.resources.access import kind_dict

    return resolve_from_dict_with_provenance(
        kind_dict(registry, "agent-template"),
        template_name,
        overlay=overlay,
        instance_name=instance_name,
    )


def resolve_live_template(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> ResolvedAgentTemplate:
    """Resolve a persisted agent's template chain plus its stored final layer."""
    from typing import cast

    from agentworks.instance_specs import get_instance_overlay

    overlay = get_instance_overlay(db, "agent", instance_name)
    if overlay is None:
        return resolve_template(registry, template_name)
    return resolve_template(
        registry,
        template_name,
        overlay=cast("AgentTemplate", overlay.declaration),
        instance_name=instance_name,
    )


def resolve_live_template_with_provenance(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> LayeredResolution[ResolvedAgentTemplate]:
    """Resolve a persisted agent and retain its layer provenance."""
    from typing import cast

    from agentworks.instance_specs import get_instance_overlay

    overlay = get_instance_overlay(db, "agent", instance_name)
    return resolve_template_with_provenance(
        registry,
        template_name,
        overlay=None if overlay is None else cast("AgentTemplate", overlay.declaration),
        instance_name=instance_name,
    )


def effective_template(templates: Mapping[str, AgentTemplate], name: str) -> ResolvedAgentTemplate:
    """The effective (merged) declaration of ``name``, for the finalize
    passes. TOTAL; see ``vms/templates.effective_template`` for why the
    degradation on a cyclic chain is never observed."""
    return effective_template_with_provenance(templates, name).value


def effective_template_with_provenance(
    templates: Mapping[str, AgentTemplate],
    name: str,
) -> LayeredResolution[ResolvedAgentTemplate]:
    """The total finalize view with the provenance of every surviving value."""
    from agentworks.errors import InheritanceCycleError
    from agentworks.resources.inheritance import LayeredResolution

    try:
        return _resolve_with_provenance(templates, name)
    except InheritanceCycleError:
        return LayeredResolution(ResolvedAgentTemplate(name=name), {})


def _resolve(
    templates: Mapping[str, AgentTemplate],
    name: str,
    *,
    overlay: AgentTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedAgentTemplate:
    """Resolve ``name``'s chain, defaults applied: one accumulator folded
    over the chain's declarations. See ``vms.templates._resolve_from_dict``
    for why the fold reads the DECLARATIONS rather than each parent's
    resolved template.
    """
    return _resolve_with_provenance(
        templates,
        name,
        overlay=overlay,
        instance_name=instance_name,
    ).value


def _resolve_with_provenance(
    templates: Mapping[str, AgentTemplate],
    name: str,
    *,
    overlay: AgentTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedAgentTemplate]:
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
            LayerSource(LayerSourceKind.TEMPLATE, "agent-template", layer.name),
            layer,
        )
        for layer in resolution_layers(templates, name, "agent-template")
    ]
    if overlay is not None:
        layers.append(
            DeclarationLayer(
                LayerSource(LayerSourceKind.INSTANCE, "agent", instance_name or overlay.name),
                overlay,
            )
        )
    return run_layer_fold(
        ResolvedAgentTemplate(name=name),
        layers,
        _merge_template,
        default_paths=(
            (field,)
            for field in (
                "shell",
                "dotfiles_destination",
                "dotfiles_install_cmd",
                "mise_activate",
                "mise_allow_unlocked",
                "mise_install_before",
                "mise_prune_on_reinit",
            )
        ),
        default_resource_kind="agent-template",
        default_name=name,
    )


def _merge_template(
    target: ResolvedAgentTemplate,
    tmpl: AgentTemplate,
    _source: object,
) -> tuple[ResolvedAgentTemplate, tuple[LayerContribution, ...]]:
    """Merge one declaration, preserving the domain's ``None``-as-absence rule."""
    from agentworks.env.entry import EnvEntry
    from agentworks.instance_overlay_codec import OVERLAY_EXCLUDED_FIELDS
    from agentworks.schema import merge_model

    resolved_fields = fields(ResolvedAgentTemplate)
    previous = {field.name: getattr(target, field.name) for field in resolved_fields if field.name != "name"}
    previous["env"] = {key: entry.model_dump(mode="python") for key, entry in target.env.items()}
    dumped = tmpl.model_dump(
        mode="python",
        exclude=set(OVERLAY_EXCLUDED_FIELDS),
        exclude_unset=True,
    )
    authored = {name: value for name, value in dumped.items() if value is not None}
    merged, operations = merge_model(type(tmpl), previous, authored)
    raw = cast("dict[str, object]", merged)
    raw["env"] = {key: EnvEntry.model_validate(value) for key, value in cast("dict[str, object]", raw["env"]).items()}
    raw["name"] = target.name
    return (
        ResolvedAgentTemplate(**cast("dict[str, Any]", raw)),
        operations,
    )
