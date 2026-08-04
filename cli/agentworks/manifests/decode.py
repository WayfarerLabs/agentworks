"""Spec decode: envelope ``spec`` -> the kind's Resource dataclass.

The decoders do NOT reimplement field validation. Each one reassembles
the shape the corresponding TOML loader consumes and calls that loader
with a fixed-location ``decls`` shim, so every type check, enum, env
entry rule, and unknown-key warning is shared verbatim between the TOML
and manifest sources. When the TOML resource surface is deleted at the
cutover, these loaders become manifest-only and can be renamed in place.

Capability-owned blobs (``provider_config``, ``platform_config``,
``harness_config``) are NOT validated here: their shape check is the
finalize ``validate`` pass (R3), which runs once over the built graph
with the Resource's source location re-attached. Decode still performs
the kind-owned spec-shape checks (a blob must be a mapping, a field may
not shadow kind-owned surface) and re-attaches the TRUE blob to the
decl (the loader's flat shape drops keys it doesn't know), so the
finalize pass sees every capability field.

``KIND_SECTIONS`` maps kind identifiers to their legacy TOML section
names; it is the shared table the manifest migrator consumes so the two
sides cannot disagree about what maps to what.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agentworks.errors import AgentworksError, ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import _SectionLineMap
    from agentworks.manifests.envelope import Document
    from agentworks.source_location import SourceLocation


# Kind identifier -> legacy TOML section name(s) (the migrator's table).
# Every kind maps to exactly one section except vm-site, whose legacy
# declarations are the two flat sections [azure] / [proxmox] with
# section-name-becomes-resource-name semantics.
KIND_SECTIONS: dict[str, tuple[str, ...]] = {
    "secret": ("secrets",),
    "vm-template": ("vm_templates",),
    "agent-template": ("agent_templates",),
    "workspace-template": ("workspace_templates",),
    "session-template": ("session_templates",),
    "git-credential": ("git_credentials",),
    "admin-template": ("admin",),
    "named-console-template": ("named_console",),
    # secret-backend: capability kind, not declarable (no decoder);
    # listed for the migrator's [secret_backends.*] drop handling only.
    "secret-backend": ("secret_backends",),
    "vm-site": ("azure", "proxmox"),
    "apt-source": ("apt_sources",),
    "apt-package": ("apt_packages",),
    "system-install-command": ("system_install_commands",),
    "user-install-command": ("user_install_commands",),
}


# Kind identifier -> (naming field, sibling config field) for the three
# hosting surfaces whose spec selects a capability and hands it a config
# blob. The canonical manifest shape is one tagged table on the naming
# field (``platform: {name: lima, vm_host: ...}``; ``name`` selects the
# capability, the remaining keys are its config). The old sibling shape
# (``platform: lima`` plus ``platform_config: {...}``) still loads but
# is deprecated for removal; ``_normalize_capability_field`` folds the
# tagged shape back into the sibling pair so the decoders and the shared
# TOML loaders underneath them see one internal shape. The secret kind's
# ``backend_mappings`` is not listed: its map key already names the
# capability.
CAPABILITY_FIELDS: dict[str, tuple[str, str]] = {
    "vm-site": ("platform", "platform_config"),
    "git-credential": ("provider", "provider_config"),
}


def _normalize_session_harness_selector(spec: dict[str, object]) -> bool:
    """Normalize the 0.13 harness selector compatibility boundary.

    This function is deliberately the only place where old YAML selector
    input is accepted. Both spellings leave this boundary as the canonical
    internal pair.
    """
    old_fields = {"harness", "harness_config"} & set(spec)
    new_fields = {"harness_integration", "harness_integration_config"} & set(spec)
    if old_fields and new_fields:
        names = ", ".join(sorted(old_fields | new_fields))
        raise ConfigError(
            f"old and new harness selector/config fields cannot be mixed: {names}; "
            "use harness_integration: {name: ..., <config keys...>} only"
        )
    if old_fields:
        if "harness_config" in spec and "harness" not in spec:
            raise ConfigError(
                "deprecated spec.harness_config needs a spec.harness selector; "
                "use a spec.harness_integration tagged table with name: shell"
            )
        value = spec.pop("harness", None)
        if isinstance(value, dict):
            if "harness_config" in spec:
                raise ConfigError(
                    "spec.harness is a tagged table, so a sibling spec.harness_config is ambiguous; "
                    "fold those keys into a spec.harness_integration tagged table"
                )
            name = value.get("name")
            if not isinstance(name, str) or not name:
                raise ConfigError(
                    "deprecated spec.harness table requires a 'name' key; "
                    "use a spec.harness_integration tagged table with name: shell"
                )
            config = {key: item for key, item in value.items() if key != "name"}
            spec["harness_integration"] = name
            if config:
                spec["harness_integration_config"] = config
        else:
            if not isinstance(value, str) or not value:
                raise ConfigError(
                    "deprecated spec.harness must be a non-empty string or tagged table; "
                    "use a spec.harness_integration tagged table with name: shell"
                )
            spec["harness_integration"] = value
            if "harness_config" in spec:
                spec["harness_integration_config"] = spec.pop("harness_config")
        return True
    # A template may intentionally declare no workload here: it inherits a
    # selector from a parent, or remains the default login shell. Preserve
    # that established no-selector form rather than treating absence as an
    # invalid canonical selector.
    if not new_fields:
        return False
    value = spec.pop("harness_integration", None)
    if "harness_integration_config" in spec:
        raise ConfigError(
            "spec.harness_integration is a tagged table; spec.harness_integration_config is not a supported YAML field"
        )
    if not isinstance(value, dict):
        raise ConfigError("spec.harness_integration must be a tagged table with a string 'name' key")
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError("spec.harness_integration (table form) requires a 'name' key naming the capability")
    config = {key: item for key, item in value.items() if key != "name"}
    spec["harness_integration"] = name
    if config:
        spec["harness_integration_config"] = config
    return False


def _normalize_capability_field(kind: str, spec: dict[str, object]) -> bool:
    """Fold the tagged capability table into the internal sibling pair.

    Returns True when the document spelled the OLD (deprecated) sibling
    shape: the naming field as a string, with or without the sibling
    ``*_config`` table. Mixing the shapes (a tagged table plus a sibling
    ``*_config``) is ambiguous and errors. Shapes this function does not
    recognize (a missing or non-string, non-mapping naming field) pass
    through untouched to the surface's own error paths.
    """
    pair = CAPABILITY_FIELDS.get(kind)
    if pair is None:
        return False
    field, config_field = pair
    value = spec.get(field)
    if isinstance(value, dict):
        if config_field in spec:
            raise ConfigError(
                f"spec.{field} is a tagged table (its 'name' key selects the "
                f"capability and the other keys are its config), so a sibling "
                f"spec.{config_field} is ambiguous; fold those keys into the "
                f"spec.{field} table"
            )
        name = value.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"spec.{field} (table form) requires a 'name' key naming the capability",
            )
        config = {key: item for key, item in value.items() if key != "name"}
        spec[field] = name
        if config:
            spec[config_field] = config
        return False
    return isinstance(value, str)


def capability_shape_deprecation(resources: list[str]) -> str:
    """The ONE aggregated deprecation message for old-shape documents.

    ``resources`` are ``kind/name`` display tokens, in load order.
    Aggregated for the same reason the TOML resource-section nudge is
    (``_warn_deprecated_resource_sections``): one warning per document
    would be obnoxious on real configs.
    """
    return (
        f"deprecated capability config shape in: {', '.join(resources)}. "
        f"Naming a capability as a string with its config in a sibling table "
        f"(platform: lima plus platform_config:, and likewise "
        f"provider/provider_config and harness/harness_config) is deprecated "
        f"and will be removed in a future release. Fold the pair into one "
        f"tagged table on the naming field, e.g. "
        f"platform: {{name: <capability>, <config keys...>}} replacing "
        f"platform: <capability> plus platform_config:. "
        f"Silence this warning with --no-deprecations."
    )


class _FixedDecls:
    """Duck-typed stand-in for config's ``_SectionLineMap``: every lookup
    resolves to the manifest document's own location.
    """

    def __init__(self, location: SourceLocation) -> None:
        self._location = location
        self.config_path = location.file

    def lookup(self, *_path: str) -> SourceLocation:
        return self._location


def _decls(location: SourceLocation) -> _SectionLineMap:
    """The duck-typed shim, cast to the loaders' declared type. The
    loaders only call ``lookup`` (and read ``config_path``); the shared
    contract is structural, not nominal.
    """
    return cast("_SectionLineMap", _FixedDecls(location))


def decode_document(
    doc: Document,
    issues: list[str],
    deprecated_shapes: list[str] | None = None,
    deprecated_harness_selectors: list[str] | None = None,
    deprecated_restart_commands: list[str] | None = None,
) -> Any:
    """Decode one validated envelope into the kind's Resource instance.

    Spec-level warnings (unknown keys on warn-mode kinds, env hygiene)
    are appended to ``issues`` prefixed with the document location.
    Spec-level errors re-raise as ``ConfigError`` with the same prefix.
    When the document spells the deprecated sibling capability-config
    shape, its ``kind/name`` is appended to ``deprecated_shapes`` (when
    given); the caller aggregates the collected tokens into the ONE
    deprecation warning (``capability_shape_deprecation``).
    """
    decoder = _DECODERS[doc.kind]
    spec = dict(doc.spec)
    # Every declarable kind carries a description field now (the nine
    # full-shape resources via DeclaredResource, the four apt /
    # install-command entries on their own), so the envelope's
    # metadata.description is injected unconditionally: the shared
    # loaders validate and attach it exactly as for TOML. Description
    # belongs in metadata, never in spec.
    if "description" in spec:
        raise ConfigError(
            f"{doc.where}: description belongs in metadata.description, not in spec",
        )
    if doc.description is not None:
        spec["description"] = doc.description

    local_issues: list[str] = []
    try:
        # General deprecated-field notices (FRD R11), run before per-kind
        # delegation and kept decoupled from schema validation so the
        # whole shim is removable (delete deprecated_fields.py and this
        # call). Error-level fields raise here, never reaching the loader;
        # warn-level fields add a notice and fall through, ignored.
        from agentworks.manifests.deprecated_fields import check_deprecated_fields

        local_issues.extend(check_deprecated_fields(doc.kind, spec))
        # Capability-shape normalization (tagged table -> internal
        # sibling pair) runs next, so every decoder and the shared TOML
        # loaders underneath see exactly one shape. Old-shape usage is
        # collected for the caller's aggregated deprecation warning.
        if doc.kind == "session-template":
            if _normalize_session_harness_selector(spec) and deprecated_harness_selectors is not None:
                deprecated_harness_selectors.append(f"{doc.kind}/{doc.name}")
            config = spec.get("harness_integration_config")
            if (
                spec.get("harness_integration") == "shell"
                and isinstance(config, dict)
                and "restart_command" in config
                and deprecated_restart_commands is not None
            ):
                deprecated_restart_commands.append(f"{doc.kind}/{doc.name}")
        elif _normalize_capability_field(doc.kind, spec) and deprecated_shapes is not None:
            deprecated_shapes.append(f"{doc.kind}/{doc.name}")
        resource = decoder(doc, spec, local_issues)
    except AgentworksError as exc:
        # A spec-level failure from any loader (the apt / install-command
        # loaders raise ConfigError directly; others raise their own
        # AgentworksError subtype) is, from a manifest, an operator-config
        # mistake, so it re-raises as ConfigError with the document
        # location, per the LLD's error catalog.
        raise ConfigError(f"{doc.where}: {exc}", hint=exc.hint) from exc
    issues.extend(f"{doc.where}: {issue}" for issue in local_issues)
    return resource


def _decode_secret(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_secrets

    result = _load_secrets({"secrets": {doc.name: spec}}, issues, _decls(doc.location))
    return result[doc.name]


def _decode_vm_template(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_vm_templates

    result = _load_vm_templates({"vm_templates": {doc.name: spec}}, issues, _decls(doc.location))
    return result[doc.name]


def _decode_agent_template(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_agent_templates

    result = _load_agent_templates({"agent_templates": {doc.name: spec}}, issues, _decls(doc.location))
    return result[doc.name]


def _decode_workspace_template(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_workspace_templates

    result = _load_workspace_templates({"workspace_templates": {doc.name: spec}}, issues, _decls(doc.location))
    return result[doc.name]


def _decode_session_template(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_session_templates

    # The YAML spec is clean (FRD R2): the legacy flat fields are
    # ``shell``'s config vocabulary and live only under
    # harness_integration_config.
    # A manifest that spells them top-level is rejected (pointing at the
    # nested shape) by the general deprecated-field table (FRD R11),
    # consulted in decode_document before this decoder runs, so no
    # bespoke check lives here.
    harness_integration_config = spec.get("harness_integration_config")
    if harness_integration_config is not None and not isinstance(harness_integration_config, dict):
        raise ConfigError("spec.harness_integration_config must be a mapping")
    # The harness_integration_config blob's shape is validated by the finalize
    # ``validate`` pass (SessionTemplate.validate), not here: capability
    # validation is decoupled from decode (R3). The mapping-shape check
    # above is kind-owned decode structure and stays at load.
    result = _load_session_templates({"session_templates": {doc.name: spec}}, issues, _decls(doc.location))
    return result[doc.name]


def _decode_git_credential(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_git_credentials

    if "type" in spec:
        raise ConfigError(
            'git-credential manifests use "provider", not "type"',
        )
    provider = spec.pop("provider", None)
    if not isinstance(provider, str) or not provider:
        raise ConfigError(
            "git-credential requires spec.provider (github or azdo)",
        )
    # Kind-owned fields stay top-level; provider-owned configuration
    # (azdo's org) rides the provider table (or the deprecated sibling
    # provider_config). The YAML shape deliberately diverges from the
    # flat TOML sections here: the decoder flattens back into the shared
    # loader's shape, so validation stays verbatim-shared with TOML.
    raw_config = spec.pop("provider_config", {})
    if not isinstance(raw_config, dict):
        raise ConfigError("spec.provider_config must be a mapping")
    # The flatten-into-the-loader trick must not let the blob shadow
    # kind-owned surface: without this check, provider_config.type/
    # provider would silently re-pick the provider. ``token`` is NOT
    # reserved: it is provider-owned config now (the secret the
    # provider sources its PAT from), and lives under provider_config.
    reserved = {"type", "provider", "description"} & set(raw_config)
    if reserved:
        names = ", ".join(sorted(reserved))
        # Shape-neutral wording: the config arrives from the tagged
        # provider table or the deprecated sibling provider_config, so
        # the message names neither field.
        raise ConfigError(
            f"the provider config may not contain kind-owned field(s): {names}; they belong at the spec top level"
        )
    if "token" in spec:
        raise ConfigError(
            "git-credential 'token' is provider config now: move it into "
            "the spec.provider table (its 'name' key selects the provider)"
        )
    loader_spec: dict[str, object] = {"type": provider, **raw_config}
    if "description" in spec:
        loader_spec["description"] = spec.pop("description")
    if spec:
        extras = ", ".join(sorted(spec))
        raise ConfigError(
            f"unknown git-credential spec field(s): {extras}; "
            "provider-specific configuration (e.g. azdo's org) goes inside "
            "the spec.provider table"
        )
    # The TRUE blob's shape is validated by the finalize ``validate``
    # pass (GitCredentialConfig.validate), not here: capability
    # validation is decoupled from decode (R3). The full blob is
    # re-attached to the decl below so the finalize pass sees every
    # capability field (the loader flatten drops keys it doesn't know).
    # The spec-shape checks above stay at load (kind-owned decode
    # structure).
    result = _load_git_credentials(
        {"git_credentials": {doc.name: loader_spec}},
        issues,
        _decls(doc.location),
        # The flatten passes blob keys through the loader shape, but the
        # TRUE blob is re-attached below; scopes ARE honored on this
        # path, so the TOML-only ignored-scope warning must not fire.
        warn_ignored_scope_keys=False,
    )[doc.name]
    # The loader flatten only carries the blob columns the legacy TOML
    # shape knows (org); re-attach the full blob so manifest rows keep
    # every capability field (reference derivation and the finalize
    # ``validate`` pass both read it). TOML rows keep the loader's blob:
    # the flat domain cannot express richer capability config.
    if raw_config:
        import dataclasses

        result = dataclasses.replace(result, provider_config=dict(raw_config))
    return result


def _decode_vm_site(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import MAX_FREEFORM_NAME_LENGTH, validate_name
    from agentworks.vms.sites import VMSiteDecl

    # Site names hit no OS-level identifier limit: they are a registry key and
    # display/config surface only, never derived into a hostname or SSH host
    # alias (VM names, not site names, feed {slug}-{vm} hostnames). So they take
    # the freeform cap, not the tighter VM-name cap.
    validate_name(doc.name, max_length=MAX_FREEFORM_NAME_LENGTH)
    platform = spec.pop("platform", None)
    if not isinstance(platform, str) or not platform:
        raise ConfigError(
            "vm-site requires spec.platform (a vm-platform capability name, "
            "e.g. lima, wsl2, azure-vm, aws-ec2, proxmox)",
        )
    raw_config = spec.pop("platform_config", {})
    if not isinstance(raw_config, dict):
        raise ConfigError("spec.platform_config must be a mapping")
    # ``token_secret`` (proxmox) is a bare operator-supplied secret name; warn
    # (only) when it is present and non-conforming, matching the other
    # reference sites. The location is kept relative; the decode layer prefixes
    # ``doc.where``. A non-string / empty shape is left to the finalize
    # ``validate`` pass (ProxmoxPlatform.validate), not re-checked here. The
    # check is not platform-gated (unlike the legacy TOML loader, whose
    # per-section known_keys naturally scope it): a stray ``token_secret`` on a
    # non-proxmox site would also warn, which is harmless because that site's
    # ``validate`` rejects the unknown field at finalize regardless.
    from agentworks.config.loaders_core import _warn_nonconforming_secret_name

    token_secret = raw_config.get("token_secret")
    if isinstance(token_secret, str) and token_secret:
        # Shape-neutral location: the key arrives from the tagged
        # platform table or the deprecated sibling platform_config.
        _warn_nonconforming_secret_name(token_secret, location="token_secret (platform config)", issues=issues)
    # The blob may not shadow kind-owned surface (the git-credential
    # precedent): platform/description in the blob would silently
    # re-pick the capability or override metadata.
    reserved = {"platform", "description"} & set(raw_config)
    if reserved:
        names = ", ".join(sorted(reserved))
        # Shape-neutral wording: the config arrives from the tagged
        # platform table or the deprecated sibling platform_config, so
        # the message names neither field.
        raise ConfigError(
            f"the platform config may not contain kind-owned field(s): {names}; they belong at the spec top level"
        )
    description = spec.pop("description", None)
    if spec:
        extras = ", ".join(sorted(spec))
        raise ConfigError(
            f"unknown vm-site spec field(s): {extras}; "
            "platform-specific configuration goes inside the spec.platform table"
        )
    # The platform_config blob's shape is validated by the finalize
    # ``validate`` pass (VMSiteDecl.validate), not here: capability
    # validation is decoupled from decode (R3). The shadow check below is
    # kind-owned decode structure and stays at load. Unknown platform
    # names are tolerated: the site registers and self-disables
    # ("platform 'x' is not installed"); a plugin's platform may simply
    # not be here.
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

    # A site named after a known platform must declare that
    # platform; `vm-site/azure-vm` backed by lima would make every
    # `--site azure-vm` mean something other than it says.
    if doc.name in VM_PLATFORM_REGISTRY and platform != doc.name:
        raise ConfigError(
            f"a vm-site named '{doc.name}' must declare platform "
            f"'{doc.name}' (it shadows a platform name), not '{platform}'"
        )
    return VMSiteDecl(
        name=doc.name,
        platform=platform,
        platform_config=dict(raw_config),
        description=str(description) if description is not None else None,
        declared_at=doc.location,
    )


def _decode_admin_template(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_admin_config

    body = dict(spec)
    env = body.pop("env", {})
    result = _load_admin_config(
        {"admin": {"config": body, "env": env}},
        issues,
        _decls(doc.location),
        name=doc.name,
    )
    assert result is not None  # the key is always present on this path
    return result


def _decode_named_console_template(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_named_console

    result = _load_named_console({"named_console": spec}, issues, _decls(doc.location))
    assert result is not None  # the key is always present on this path
    return result


def _decode_apt_source(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.apt import _load_apt_sources

    return _load_apt_sources({doc.name: spec}, _decls(doc.location))[doc.name]


def _decode_apt_package(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.apt import _load_apt_packages

    return _load_apt_packages({doc.name: spec}, _decls(doc.location))[doc.name]


def _decode_system_install_command(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.install_commands import _load_system_commands

    return _load_system_commands({doc.name: spec}, _decls(doc.location))[doc.name]


def _decode_user_install_command(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.install_commands import _load_user_commands

    return _load_user_commands({doc.name: spec}, _decls(doc.location))[doc.name]


_DECODERS: dict[str, Callable[[Document, dict[str, object], list[str]], Any]] = {
    "secret": _decode_secret,
    "vm-template": _decode_vm_template,
    "agent-template": _decode_agent_template,
    "workspace-template": _decode_workspace_template,
    "session-template": _decode_session_template,
    "git-credential": _decode_git_credential,
    "vm-site": _decode_vm_site,
    "admin-template": _decode_admin_template,
    "named-console-template": _decode_named_console_template,
    "apt-source": _decode_apt_source,
    "apt-package": _decode_apt_package,
    "system-install-command": _decode_system_install_command,
    "user-install-command": _decode_user_install_command,
}
