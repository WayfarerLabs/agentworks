"""Admin-template selection and per-VM final-layer resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import ValidationError as PydanticValidationError

from agentworks.errors import unknown_template_error

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.db import Database
    from agentworks.resources.inheritance import LayerContribution, LayeredResolution
    from agentworks.resources.registry import Registry
    from agentworks.vms.admin import AdminConfig


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

    selected = "default" if template_name is None else template_name
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
        default_paths=(
            ("username",),
            ("shell",),
            ("dotfiles_source",),
            ("dotfiles_destination",),
            ("dotfiles_install_cmd",),
            ("mise_activate",),
            ("mise_lockfile",),
            ("mise_allow_unlocked",),
            ("mise_install_before",),
            ("mise_prune_on_reinit",),
            ("git_force_safe_directory",),
        ),
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
            custom_error_sanitizer=_safe_mise_validation_message,
        ) from None
    return LayeredResolution(effective, layered.provenance)


def _safe_mise_validation_message(error: object) -> str | None:
    """Map stable mise categories to value-safe operator reasons."""
    from agentworks.config.validation import MiseSettingsError, MiseSettingsErrorKind

    if not isinstance(error, MiseSettingsError):
        return None
    if error.kind is MiseSettingsErrorKind.PACKAGE_SYNTAX:
        return "mise_packages entries must use non-empty name@version syntax"
    if error.kind is MiseSettingsErrorKind.LOCKFILE:
        return "mise_lockfile is invalid"
    if error.kind is MiseSettingsErrorKind.INSTALL_BEFORE:
        return "mise_install_before must be a positive duration such as '7d' or an ISO date"
    return None


def resolve_live_template(
    db: Database,
    registry: Registry,
    instance_name: str,
    template_name: str | None,
) -> AdminConfig:
    """Resolve a VM's admin template plus its stored final admin layer."""
    from agentworks.instance_specs import get_vm_instance_overlays

    overlays = get_vm_instance_overlays(db, instance_name)
    return resolve_template_with_provenance(
        registry,
        template_name,
        overlay=None if overlays is None else overlays.admin,
        instance_name=instance_name,
    ).value


def _merge_template(
    target: AdminConfig,
    layer: AdminConfig,
    _source: object,
) -> tuple[AdminConfig, tuple[LayerContribution, ...]]:
    """Merge only fields explicitly authored by this admin declaration."""
    from agentworks.env.entry import EnvEntry
    from agentworks.instance_overlay_codec import OVERLAY_EXCLUDED_FIELDS
    from agentworks.schema import merge_model

    previous = target.model_dump(
        mode="python",
        exclude=set(OVERLAY_EXCLUDED_FIELDS),
    )
    authored = layer.model_dump(
        mode="python",
        exclude=set(OVERLAY_EXCLUDED_FIELDS),
        exclude_unset=True,
    )
    merged, operations = merge_model(type(layer), previous, authored)
    raw = cast("dict[str, object]", merged)
    raw["env"] = {key: EnvEntry.model_validate(value) for key, value in cast("dict[str, object]", raw["env"]).items()}
    return target.model_copy(update=raw), operations
