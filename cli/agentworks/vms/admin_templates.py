"""Admin-template selection and per-VM final-layer resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError as PydanticValidationError

from agentworks.errors import unknown_template_error

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.db import Database
    from agentworks.resources.inheritance import LayerContribution, LayeredResolution
    from agentworks.resources.registry import Registry
    from agentworks.vms.admin import AdminConfig


def resolve_template(
    registry: Registry,
    template_name: str | None = None,
    *,
    overlay: AdminConfig | None = None,
    instance_name: str | None = None,
) -> AdminConfig:
    """Resolve one admin template and an optional final per-VM layer."""
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
    overlay: AdminConfig | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[AdminConfig]:
    """Resolve an admin template while retaining reference provenance."""
    from agentworks.resources.access import kind_dict

    return resolve_from_dict_with_provenance(
        kind_dict(registry, "admin-template"),
        template_name,
        overlay=overlay,
        instance_name=instance_name,
    )


def resolve_from_dict_with_provenance(
    templates: Mapping[str, AdminConfig],
    template_name: str | None = None,
    *,
    overlay: AdminConfig | None = None,
    instance_name: str | None = None,
) -> LayeredResolution[AdminConfig]:
    """Resolve an admin template from typed rows and retain provenance."""
    from agentworks.resources.inheritance import (
        DeclarationLayer,
        LayeredResolution,
        LayerSource,
        LayerSourceKind,
        run_layer_fold,
    )
    from agentworks.vms.admin import AdminConfig

    selected = template_name or "default"
    declared = templates.get(selected)
    if declared is None and selected != "default":
        raise unknown_template_error(
            kind="admin-template",
            label="admin template",
            name=selected,
            available=templates,
        ) from None
    if declared is None:
        declared = AdminConfig()

    layers = [
        DeclarationLayer(
            LayerSource(LayerSourceKind.TEMPLATE, "admin-template", selected),
            declared,
        )
    ]
    if overlay is not None:
        layers.append(
            DeclarationLayer(
                LayerSource(LayerSourceKind.INSTANCE, "vm", instance_name or overlay.name),
                overlay,
            )
        )
    layered = run_layer_fold(
        AdminConfig(name=selected),
        layers,
        _merge_template,
        default_resource_kind="admin-template",
        default_name=selected,
    )
    try:
        from agentworks.instance_overlay_codec import OVERLAY_EXCLUDED_FIELDS

        effective = AdminConfig.model_validate(
            {
                "name": selected,
                **layered.value.model_dump(
                    mode="python",
                    exclude=set(OVERLAY_EXCLUDED_FIELDS),
                ),
            }
        )
    except PydanticValidationError as error:
        from agentworks.instance_overlay_codec import value_safe_model_validation_error

        raise value_safe_model_validation_error(
            error,
            "invalid effective VM admin spec",
            entity_kind="vm",
            entity_name=instance_name,
            classify_unsupported=False,
        ) from None
    return LayeredResolution(effective, layered.provenance)


def resolve_live_template(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> AdminConfig:
    """Resolve a VM's admin template plus its stored final admin layer."""
    return resolve_live_template_with_provenance(db, registry, instance_name, template_name).value


def resolve_live_template_with_provenance(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> LayeredResolution[AdminConfig]:
    """Resolve a persisted VM admin declaration with layer provenance."""
    from agentworks.instance_specs import get_vm_instance_overlays

    overlays = get_vm_instance_overlays(db, instance_name)
    return resolve_template_with_provenance(
        registry,
        template_name,
        overlay=None if overlays is None else overlays.admin,
        instance_name=instance_name,
    )


def _append_dedupe(target: list[str], source: list[str]) -> list[str]:
    seen = set(target)
    result = list(target)
    for item in source:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _merge_template(
    target: AdminConfig,
    layer: AdminConfig,
    _source: object,
) -> tuple[AdminConfig, tuple[LayerContribution, ...]]:
    """Fold fields explicitly declared by an admin-template or final layer."""
    from agentworks.resources.inheritance import LayerContribution

    supplied = layer.model_fields_set
    touched: list[LayerContribution] = []
    updates: dict[str, object] = {}
    for field in (
        "username",
        "shell",
        "dotfiles_source",
        "dotfiles_destination",
        "dotfiles_install_cmd",
        "mise_activate",
        "mise_lockfile",
        "mise_allow_unlocked",
        "mise_install_before",
        "mise_prune_on_reinit",
        "git_force_safe_directory",
    ):
        if field in supplied:
            updates[field] = getattr(layer, field)
            touched.append(LayerContribution.replacement(field))

    for field in (
        "git_credentials",
        "user_install_commands",
        "mise_packages",
        "claude_marketplaces",
        "claude_plugins",
    ):
        if field in supplied:
            values = getattr(layer, field)
            updates[field] = _append_dedupe(getattr(target, field), values)
            touched.extend(LayerContribution.contribution(field, item) for item in values)

    if "env" in supplied:
        updates["env"] = {**target.env, **layer.env}
        touched.extend(LayerContribution.replacement("env", key) for key in layer.env)
    return target.model_copy(update=updates), tuple(touched)
