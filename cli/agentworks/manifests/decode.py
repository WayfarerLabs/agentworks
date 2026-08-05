"""Spec decode: envelope ``spec`` -> the kind's Resource dataclass.

Each decoder owns its per-kind field validation directly (config.toml no
longer declares resources, ADR 0022, so the manifest decoders no longer
route through the shared TOML loaders; those loaders relocated to the
migrator's private oracle, ``agentworks.migrate.toml_resources``). This
forks the per-kind assembly with the oracle: the two carry near-duplicate
validation on purpose, so the migrator's registry-equivalence check is a
real test of the emission mapping rather than a tautology. Phase 2's kind
spec models dissolve this decoder side. The decoders share the leaf
validators (``_warn_unexpected_keys``, ``_parse_env_table``, the two
nonconforming-secret-name helpers, ``validate_name``) with the oracle, so
the fork stays narrow. The apt / install-command decoders are the one
exception: their emission is trivial envelope-wrapping, so they still
delegate to the ``agentworks.apt`` / ``agentworks.install_commands`` domain
loaders (imported from there, not from the relocated oracle).

Capability-owned blobs (``provider_config``, ``platform_config``,
``harness_integration_config``) are NOT validated here: their shape check
is the finalize ``validate`` pass (R3). Decode still performs the
kind-owned spec-shape checks (a blob must be a mapping, a field may not
shadow kind-owned surface) and attaches the TRUE blob to the decl, so the
finalize pass sees every capability field.

``KIND_SECTIONS`` maps kind identifiers to their legacy TOML section
names; it is the shared table the manifest migrator consumes so the two
sides cannot disagree about what maps to what.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentworks.errors import AgentworksError, ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.config import _SectionLineMap
    from agentworks.env import EnvEntry
    from agentworks.manifests.envelope import Document


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


def _doc_decls(section: str, doc: Document) -> _SectionLineMap:
    """A ``_SectionLineMap`` resolving ``(section, doc.name)`` to the
    document's own location.

    A manifest document has one position, not a per-section line map, so
    this seeds the map with the single ``(section, name)`` entry the apt /
    install-command domain loaders look up. Those loaders (in
    ``agentworks.apt`` / ``agentworks.install_commands``) are the only
    decoders that still delegate rather than owning their assembly (their
    emission is trivial envelope-wrapping); the full-shape decoders below
    build their decls directly.
    """
    from agentworks.config import _SectionLineMap

    return _SectionLineMap(
        config_path=doc.location.file,
        section_lines={(section, doc.name): doc.location.line},
    )


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
                and "resume_command" in config
                and "restart_command" in config
            ):
                raise ConfigError("resume_command and restart_command cannot be combined; use resume_command only")
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


_SECRET_KEYS = {"description", "hint", "backend_mappings"}


def _decode_secret(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from typing import Literal

    from agentworks.config.loaders_core import _warn_unexpected_keys
    from agentworks.config.validation import MAX_SECRET_NAME_LENGTH, validate_name
    from agentworks.secrets import SecretDecl

    name = doc.name
    # Single validation point for the secret kind (secret names use the
    # larger cap; they are never derived into Linux usernames).
    validate_name(name, max_length=MAX_SECRET_NAME_LENGTH)
    _warn_unexpected_keys(spec, _SECRET_KEYS, f"secrets.{name}", issues)

    description = spec.get("description")
    if not isinstance(description, str) or not description:
        raise ConfigError(f"secrets.{name}.description is required and must be a non-empty string")
    hint = spec.get("hint")
    if hint is not None and not isinstance(hint, str):
        raise ConfigError(f"secrets.{name}.hint must be a string")

    raw_mappings = spec.get("backend_mappings", {})
    if not isinstance(raw_mappings, dict):
        raise ConfigError(f"secrets.{name}.backend_mappings must be a table")
    backend_mappings: dict[str, str | dict[str, object] | Literal[False]] = {}
    for kind, mapping in raw_mappings.items():
        kind_str = str(kind)
        if isinstance(mapping, bool):
            if mapping is True:
                raise ConfigError(
                    f"secrets.{name}.backend_mappings.{kind_str}: "
                    "boolean must be `false` (opt-out); `true` is not a valid value"
                )
            backend_mappings[kind_str] = False
        elif isinstance(mapping, str):
            backend_mappings[kind_str] = mapping
        elif isinstance(mapping, dict):
            backend_mappings[kind_str] = dict(mapping)
        else:
            raise ConfigError(f"secrets.{name}.backend_mappings.{kind_str}: must be a string, inline table, or false")

    return SecretDecl(
        name=name,
        description=description,
        hint=hint,
        backend_mappings=backend_mappings,
        declared_at=doc.location,
    )


_VM_TEMPLATE_KEYS = {
    "inherits",
    "description",
    "cpus",
    "memory",
    "disk",
    "swap",
    "apt",
    "apt_packages",
    "snap",
    "system_install_commands",
    "env",
    "tailscale_auth_key",
}


def _decode_vm_template(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.config.loaders_core import (
        _parse_env_table,
        _warn_nonconforming_secret_name,
        _warn_unexpected_keys,
    )
    from agentworks.vms.template import VMTemplate

    name = doc.name
    _warn_unexpected_keys(spec, _VM_TEMPLATE_KEYS, f"vm_templates.{name}", issues)

    ts_key_raw: str | None = None
    if "tailscale_auth_key" in spec:
        if not isinstance(spec["tailscale_auth_key"], str):
            raise ConfigError(
                f"vm_templates.{name}.tailscale_auth_key must be a bare secret "
                f"name (string), got {type(spec['tailscale_auth_key']).__name__}"
            )
        if not spec["tailscale_auth_key"]:
            raise ConfigError(
                f"vm_templates.{name}.tailscale_auth_key must not be empty; "
                f"omit the key to inherit the default secret name "
                f'"tailscale-auth-key"'
            )
        ts_key_raw = spec["tailscale_auth_key"]
        _warn_nonconforming_secret_name(ts_key_raw, location=f"vm_templates.{name}.tailscale_auth_key", issues=issues)

    return VMTemplate(
        name=name,
        inherits=list(spec.get("inherits", [])),
        description=str(spec["description"]) if "description" in spec else None,
        cpus=int(spec["cpus"]) if "cpus" in spec else None,
        memory=int(spec["memory"]) if "memory" in spec else None,
        disk=int(spec["disk"]) if "disk" in spec else None,
        swap=int(spec["swap"]) if "swap" in spec else None,
        apt=list(spec["apt"]) if "apt" in spec else None,
        apt_packages=list(spec["apt_packages"]) if "apt_packages" in spec else None,
        snap=list(spec["snap"]) if "snap" in spec else None,
        system_install_commands=(list(spec["system_install_commands"]) if "system_install_commands" in spec else None),
        tailscale_auth_key=ts_key_raw,
        env=_parse_env_table(spec.get("env"), context=f"vm_templates.{name}", issues=issues),
        declared_at=doc.location,
    )


_USER_CONFIG_KEYS = {
    "description",
    "username",
    "shell",
    "git_credentials",
    "user_install_commands",
    "dotfiles_source",
    "dotfiles_destination",
    "dotfiles_install_cmd",
    "mise_activate",
    "mise_packages",
    "mise_lockfile",
    "mise_allow_unlocked",
    "mise_install_before",
    "mise_prune_on_reinit",
    "git_force_safe_directory",
    "claude_marketplaces",
    "claude_plugins",
}
_AGENT_TEMPLATE_KEYS = _USER_CONFIG_KEYS | {"inherits", "env"}


def _decode_agent_template(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.agents.template import AgentTemplate
    from agentworks.config.loaders_core import _parse_env_table, _require_string_list, _warn_unexpected_keys

    name = doc.name
    _warn_unexpected_keys(spec, _AGENT_TEMPLATE_KEYS, f"agent_templates.{name}", issues)

    return AgentTemplate(
        name=name,
        inherits=list(spec.get("inherits", [])),
        description=str(spec["description"]) if "description" in spec else None,
        shell=str(spec["shell"]) if "shell" in spec else None,
        git_credentials=list(spec["git_credentials"]) if "git_credentials" in spec else None,
        user_install_commands=(list(spec["user_install_commands"]) if "user_install_commands" in spec else None),
        dotfiles_source=str(spec["dotfiles_source"]) if "dotfiles_source" in spec else None,
        dotfiles_destination=(str(spec["dotfiles_destination"]) if "dotfiles_destination" in spec else None),
        dotfiles_install_cmd=(str(spec["dotfiles_install_cmd"]) if "dotfiles_install_cmd" in spec else None),
        mise_activate=bool(spec["mise_activate"]) if "mise_activate" in spec else None,
        mise_packages=list(spec["mise_packages"]) if "mise_packages" in spec else None,
        mise_lockfile=str(spec["mise_lockfile"]) if "mise_lockfile" in spec else None,
        mise_allow_unlocked=(bool(spec["mise_allow_unlocked"]) if "mise_allow_unlocked" in spec else None),
        mise_install_before=(str(spec["mise_install_before"]) if "mise_install_before" in spec else None),
        mise_prune_on_reinit=(bool(spec["mise_prune_on_reinit"]) if "mise_prune_on_reinit" in spec else None),
        claude_marketplaces=(
            _require_string_list(spec, "claude_marketplaces", f"agent_templates.{name}")
            if "claude_marketplaces" in spec
            else None
        ),
        claude_plugins=(
            _require_string_list(spec, "claude_plugins", f"agent_templates.{name}")
            if "claude_plugins" in spec
            else None
        ),
        env=_parse_env_table(spec.get("env"), context=f"agent_templates.{name}", issues=issues),
        declared_at=doc.location,
    )


_WORKSPACE_TEMPLATE_KEYS = {
    "inherits",
    "description",
    "repo",
    "tmuxinator",
    "git_user_name",
    "git_user_email",
    "env",
}


def _decode_workspace_template(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.config.loaders_core import _parse_env_table, _warn_unexpected_keys
    from agentworks.workspaces.template import WorkspaceTemplate

    name = doc.name
    _warn_unexpected_keys(spec, _WORKSPACE_TEMPLATE_KEYS, f"workspace_templates.{name}", issues)
    return WorkspaceTemplate(
        name=name,
        inherits=list(spec.get("inherits", [])),
        description=str(spec["description"]) if "description" in spec else None,
        repo=str(spec["repo"]) if "repo" in spec else None,
        tmuxinator=bool(spec["tmuxinator"]) if "tmuxinator" in spec else None,
        git_user_name=(str(spec["git_user_name"]) if "git_user_name" in spec else None),
        git_user_email=(str(spec["git_user_email"]) if "git_user_email" in spec else None),
        env=_parse_env_table(spec.get("env"), context=f"workspace_templates.{name}", issues=issues),
        declared_at=doc.location,
    )


_SESSION_TEMPLATE_KEYS = {
    "inherits",
    "description",
    "harness_integration",
    "harness_integration_config",
    "env",
}


def _decode_session_template(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.config.loaders_core import _parse_env_table, _warn_unexpected_keys
    from agentworks.sessions.template import SessionTemplate

    # The selector normalization (tagged harness_integration table -> the
    # internal ``harness_integration`` name plus ``harness_integration_config``
    # blob, and the legacy ``harness`` fold) already ran in
    # ``decode_document`` before this decoder, so the spec here carries the
    # canonical internal pair. The legacy flat command fields
    # (``command`` / ``resume_command`` / ...) are ``shell``'s config
    # vocabulary and live only under harness_integration_config; a manifest
    # that spells them top-level is rejected by the deprecated-field table.
    name = doc.name
    _warn_unexpected_keys(spec, _SESSION_TEMPLATE_KEYS, f"session_templates.{name}", issues)
    harness_integration = spec.get("harness_integration")
    if harness_integration is not None and not isinstance(harness_integration, str):
        raise ConfigError(f"session_templates.{name}.harness_integration must be a string")
    raw_config = spec.get("harness_integration_config")
    if raw_config is not None and not isinstance(raw_config, dict):
        raise ConfigError("spec.harness_integration_config must be a mapping")
    if harness_integration is None and raw_config is not None:
        raise ConfigError(
            f"session_templates.{name}: harness_integration_config needs a selector "
            f'(a blob with no owner); add harness_integration = "..."'
        )
    # The deprecated ``restart_command`` shell spelling is renamed to
    # ``resume_command`` in the blob and remembered via
    # ``restart_command_compat`` (the compat marker the request boundary and
    # ``strip_source_fields`` read), mirroring the relocated loader. The
    # resume+restart conflict already raised in ``decode_document``.
    config_blob = dict(raw_config) if isinstance(raw_config, dict) else None
    uses_restart_command = (
        harness_integration == "shell" and config_blob is not None and "restart_command" in config_blob
    )
    if uses_restart_command:
        assert config_blob is not None
        config_blob["resume_command"] = config_blob.pop("restart_command")
    env: dict[str, EnvEntry] | None = None
    if "env" in spec:
        env = _parse_env_table(spec["env"], context=f"session_templates.{name}", issues=issues)
    return SessionTemplate(
        name=name,
        inherits=list(spec.get("inherits", [])),
        description=str(spec["description"]) if "description" in spec else None,
        harness_integration=harness_integration,
        harness_integration_config=config_blob,
        restart_command_compat=uses_restart_command,
        env=env,
        declared_at=doc.location,
    )


def _decode_git_credential(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.config.loaders_core import _warn_nonconforming_derived_secret, _warn_nonconforming_secret_name
    from agentworks.git_credentials.credential import GitCredentialConfig

    name = doc.name
    if "type" in spec:
        raise ConfigError(
            'git-credential manifests use "provider", not "type"',
        )
    provider = spec.pop("provider", None)
    if not isinstance(provider, str) or not provider:
        raise ConfigError(
            "git-credential requires spec.provider (github or azdo)",
        )
    # Provider-owned configuration (azdo's org, the token secret name) rides
    # the provider table (or the deprecated sibling provider_config).
    raw_config = spec.pop("provider_config", {})
    if not isinstance(raw_config, dict):
        raise ConfigError("spec.provider_config must be a mapping")
    # The blob may not shadow kind-owned surface. ``token`` is NOT reserved:
    # it is provider-owned config (the secret the provider sources its PAT
    # from) and lives under provider_config.
    reserved = {"type", "provider", "description"} & set(raw_config)
    if reserved:
        names = ", ".join(sorted(reserved))
        raise ConfigError(
            f"the provider config may not contain kind-owned field(s): {names}; they belong at the spec top level"
        )
    if "token" in spec:
        raise ConfigError(
            "git-credential 'token' is provider config now: move it into "
            "the spec.provider table (its 'name' key selects the provider)"
        )
    description = spec.pop("description", None)
    if spec:
        extras = ", ".join(sorted(spec))
        raise ConfigError(
            f"unknown git-credential spec field(s): {extras}; "
            "provider-specific configuration (e.g. azdo's org) goes inside "
            "the spec.provider table"
        )
    # Warning parity with the migrator oracle (finding 8): the token secret's
    # conformance is warned the same way the flat TOML reader does, and the
    # same empty/non-string token guard applies. When the blob sets ``token``
    # explicitly, warn on that name; otherwise warn if the derived
    # ``git-token-<name>`` default is non-conforming.
    if "token" in raw_config:
        token = raw_config["token"]
        if not isinstance(token, str):
            raise ConfigError(
                f"git_credentials.{name}.token must be a bare secret name (string), got {type(token).__name__}"
            )
        if not token:
            raise ConfigError(
                f"git_credentials.{name}.token must not be empty; "
                f"omit the key to inherit the default secret name "
                f'"git-token-{name}"'
            )
        _warn_nonconforming_secret_name(token, location=f"git_credentials.{name}.token", issues=issues)
    else:
        _warn_nonconforming_derived_secret(name, issues)
    # The TRUE blob's shape is validated by the finalize ``validate`` pass
    # (GitCredentialConfig.validate), not here (R3). The full blob is attached
    # so the finalize pass and reference derivation see every capability field.
    return GitCredentialConfig(
        name=name,
        provider=provider,
        provider_config=dict(raw_config),
        description=str(description) if description is not None else None,
        declared_at=doc.location,
    )


def _decode_vm_site(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
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


def _decode_admin_template(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.config.loaders_core import _parse_env_table, _require_string_list, _warn_unexpected_keys
    from agentworks.vms.admin import AdminConfig

    raw = dict(spec)
    env = raw.pop("env", None)
    _warn_unexpected_keys(raw, _USER_CONFIG_KEYS, "admin.config", issues)

    return AdminConfig(
        name=doc.name,
        description=str(raw["description"]) if "description" in raw else None,
        username=str(raw.get("username", "agentworks")),
        shell=str(raw.get("shell", "bash")),
        git_credentials=list(raw.get("git_credentials", [])),
        user_install_commands=list(raw.get("user_install_commands", [])),
        dotfiles_source=str(raw["dotfiles_source"]) if "dotfiles_source" in raw else None,
        dotfiles_destination=str(raw.get("dotfiles_destination", "~/.dotfiles")),
        dotfiles_install_cmd=str(raw.get("dotfiles_install_cmd", "./install.sh")),
        mise_activate=bool(raw.get("mise_activate", True)),
        mise_packages=list(raw.get("mise_packages", [])),
        mise_lockfile=str(raw["mise_lockfile"]) if "mise_lockfile" in raw else None,
        mise_allow_unlocked=bool(raw.get("mise_allow_unlocked", False)),
        mise_install_before=str(raw.get("mise_install_before", "7d")),
        mise_prune_on_reinit=bool(raw.get("mise_prune_on_reinit", True)),
        git_force_safe_directory=bool(raw.get("git_force_safe_directory", True)),
        claude_marketplaces=_require_string_list(raw, "claude_marketplaces", "admin.config"),
        claude_plugins=_require_string_list(raw, "claude_plugins", "admin.config"),
        env=_parse_env_table(env, context="admin", issues=issues),
        declared_at=doc.location,
    )


def _decode_named_console_template(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.config.loaders_core import _warn_unexpected_keys
    from agentworks.sessions.layouts import AW_SESSION_VERTICAL_LAYOUT, VALID_TMUX_LAYOUTS
    from agentworks.sessions.template import NamedConsoleConfig

    _warn_unexpected_keys(spec, {"description", "tmux_layout"}, "named_console", issues)
    layout = spec.get("tmux_layout", AW_SESSION_VERTICAL_LAYOUT)
    if layout not in VALID_TMUX_LAYOUTS:
        raise ConfigError(f"named_console.tmux_layout must be one of {VALID_TMUX_LAYOUTS}, got: {layout}")
    return NamedConsoleConfig(
        name="default",
        tmux_layout=str(layout),
        description=str(spec["description"]) if "description" in spec else None,
        declared_at=doc.location,
    )


def _decode_apt_source(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.apt import _load_apt_sources

    return _load_apt_sources({doc.name: spec}, _doc_decls("apt_sources", doc))[doc.name]


def _decode_apt_package(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.apt import _load_apt_packages

    return _load_apt_packages({doc.name: spec}, _doc_decls("apt_packages", doc))[doc.name]


def _decode_system_install_command(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.install_commands import _load_system_commands

    return _load_system_commands({doc.name: spec}, _doc_decls("system_install_commands", doc))[doc.name]


def _decode_user_install_command(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.install_commands import _load_user_commands

    return _load_user_commands({doc.name: spec}, _doc_decls("user_install_commands", doc))[doc.name]


_DECODERS: dict[str, Callable[[Document, dict[str, Any], list[str]], Any]] = {
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
