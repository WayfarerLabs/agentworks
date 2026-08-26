"""Workspace template resolution and processing.

Handles inheritance (depth-first, left-to-right), merge rules, and the
built-in empty template fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agentworks.errors import ConfigError, NotFoundError, unknown_template_error

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.db import Database
    from agentworks.env import EnvEntry
    from agentworks.resources.inheritance import LayerContribution, LayeredResolution
    from agentworks.resources.registry import Registry
    from agentworks.workspaces.template import WorkspaceTemplate


@dataclass
class ResolvedTemplate:
    """A fully resolved workspace template with all inheritance applied."""

    name: str
    repo: str | None = None
    tmuxinator: bool = True
    git_user_name: str | None = None
    git_user_email: str | None = None
    env: dict[str, EnvEntry] = field(default_factory=dict)


def resolve_from_dict(
    templates: dict[str, WorkspaceTemplate],
    template_name: str | None = None,
    *,
    overlay: WorkspaceTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedTemplate:
    """Resolve a workspace template from a templates dict.

    Selection order:
    1. Explicit template_name
    2. "default" template if it exists
    3. Built-in empty template (tmuxinator=True, no repo)
    """
    return resolve_from_dict_with_provenance(
        templates,
        template_name,
        overlay=overlay,
        instance_name=instance_name,
    ).value


def resolve_from_dict_with_provenance(
    templates: dict[str, WorkspaceTemplate],
    template_name: str | None = None,
    *,
    overlay: WorkspaceTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedTemplate]:
    if template_name is not None and template_name != "default":
        if template_name not in templates:
            raise unknown_template_error(
                kind="workspace-template",
                label="workspace template",
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
    overlay: WorkspaceTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedTemplate:
    """Resolve a workspace template by name from the Registry."""

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
    overlay: WorkspaceTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedTemplate]:
    from agentworks.resources.access import kind_dict

    return resolve_from_dict_with_provenance(
        kind_dict(registry, "workspace-template"),
        template_name,
        overlay=overlay,
        instance_name=instance_name,
    )


def resolve_live_template(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> ResolvedTemplate:
    """Resolve a persisted workspace's template chain plus its stored final layer."""
    return resolve_live_template_with_provenance(db, registry, instance_name, template_name).value


def resolve_live_template_with_provenance(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> LayeredResolution[ResolvedTemplate]:
    """Resolve a persisted workspace and retain its layer provenance."""
    from typing import cast

    from agentworks.instance_specs import get_instance_overlay

    overlay = get_instance_overlay(db, "workspace", instance_name)
    if overlay is None:
        return resolve_template_with_provenance(registry, template_name)
    declaration = cast("WorkspaceTemplate", overlay.declaration)
    try:
        return resolve_template_with_provenance(
            registry,
            template_name,
            overlay=declaration,
            instance_name=instance_name,
        )
    except NotFoundError:
        return _resolve_with_provenance(
            {},
            template_name or "default",
            overlay=declaration,
            instance_name=instance_name,
        )


def resolve_live_tmuxinator(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> bool:
    """Resolve the live tmuxinator switch without breaking copied owners.

    Historic copied workspaces use a synthetic or subsequently missing base
    template. They have always generated tmuxinator config, so preserve that
    default unless their stored final layer explicitly overrides the switch.
    Stored-overlay decoding remains strict even when the base is unavailable.
    """
    from typing import cast

    from agentworks.instance_specs import get_instance_overlay

    overlay = get_instance_overlay(db, "workspace", instance_name)
    try:
        return resolve_template(
            registry,
            template_name,
            overlay=None if overlay is None else cast("WorkspaceTemplate", overlay.declaration),
            instance_name=instance_name,
        ).tmuxinator
    except (ConfigError, NotFoundError, ValueError):
        if overlay is not None:
            declared = cast("WorkspaceTemplate", overlay.declaration).tmuxinator
            if declared is not None:
                return declared
        return True


def resolve_ws_template_env_or_empty(
    registry: Registry,
    template_name: str | None,
) -> dict[str, EnvEntry]:
    """Return config-only workspace template env, or empty when unavailable.

    Copied workspaces carry a synthetic template marker, and a stored template
    can later disappear from config. Keep this compatibility helper for callers
    that have no live workspace identity with which to load a desired overlay.
    """
    try:
        return resolve_template(registry, template_name).env
    except (ValueError, ConfigError, NotFoundError):
        return {}


def effective_template(templates: Mapping[str, WorkspaceTemplate], name: str) -> ResolvedTemplate:
    """The effective (merged) declaration of ``name``, for the finalize
    passes. TOTAL; see ``vms/templates.effective_template`` for why the
    degradation on a cyclic chain is never observed."""
    return effective_template_with_provenance(templates, name).value


def effective_template_with_provenance(
    templates: Mapping[str, WorkspaceTemplate],
    name: str,
) -> LayeredResolution[ResolvedTemplate]:
    """The total finalize view with the provenance of every surviving value."""
    from agentworks.errors import InheritanceCycleError
    from agentworks.resources.inheritance import LayeredResolution

    try:
        return _resolve_with_provenance(templates, name)
    except InheritanceCycleError:
        return LayeredResolution(ResolvedTemplate(name=name), {})


def _resolve(
    templates: Mapping[str, WorkspaceTemplate],
    name: str,
    *,
    overlay: WorkspaceTemplate | None = None,
    instance_name: str | None = None,
) -> ResolvedTemplate:
    """Resolve ``name``'s chain, defaults applied: one accumulator folded
    over the chain's declarations, last one wins. See
    ``vms.templates._resolve_from_dict`` for why the fold reads the
    DECLARATIONS rather than each parent's resolved template.
    """
    return _resolve_with_provenance(
        templates,
        name,
        overlay=overlay,
        instance_name=instance_name,
    ).value


def _resolve_with_provenance(
    templates: Mapping[str, WorkspaceTemplate],
    name: str,
    *,
    overlay: WorkspaceTemplate | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[ResolvedTemplate]:
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
            LayerSource(LayerSourceKind.TEMPLATE, "workspace-template", layer.name),
            layer,
        )
        for layer in resolution_layers(templates, name, "workspace-template")
    ]
    if overlay is not None:
        layers.append(
            DeclarationLayer(
                LayerSource(LayerSourceKind.INSTANCE, "workspace", instance_name or overlay.name),
                overlay,
            )
        )
    return run_layer_fold(
        ResolvedTemplate(name=name),
        layers,
        _merge_template,
        default_paths=(("tmuxinator",),),
        default_resource_kind="workspace-template",
        default_name=name,
    )


def _merge_template(
    target: ResolvedTemplate,
    tmpl: WorkspaceTemplate,
    _source: object,
) -> tuple[ResolvedTemplate, tuple[LayerContribution, ...]]:
    """Fold one declared WorkspaceTemplate into the accumulator. None =
    not set, skip. The only writer of a ``ResolvedTemplate``'s fields."""
    from agentworks.resources.inheritance import LayerContribution

    touched: list[LayerContribution] = []
    if tmpl.repo is not None:
        target.repo = tmpl.repo
        touched.append(LayerContribution.replacement("repo"))
    if tmpl.tmuxinator is not None:
        target.tmuxinator = tmpl.tmuxinator
        touched.append(LayerContribution.replacement("tmuxinator"))
    if tmpl.git_user_name is not None:
        target.git_user_name = tmpl.git_user_name
        touched.append(LayerContribution.replacement("git_user_name"))
    if tmpl.git_user_email is not None:
        target.git_user_email = tmpl.git_user_email
        touched.append(LayerContribution.replacement("git_user_email"))
    if tmpl.env:
        target.env = {**target.env, **tmpl.env}
        touched.extend(LayerContribution.replacement("env", key) for key in tmpl.env)
    return target, tuple(touched)
