"""Spec decode: envelope ``spec`` -> the kind's Resource dataclass.

The decoders do NOT reimplement field validation. Each one reassembles
the shape the corresponding TOML loader consumes and calls that loader
with a fixed-location ``decls`` shim, so every type check, enum, env
entry rule, and unknown-key warning is shared verbatim between the TOML
and manifest sources. When the TOML resource surface is deleted at the
cutover, these loaders become manifest-only and can be renamed in place.

Capability-owned blobs are the one deliberate exception to shared
validation: the named capability validates its ``provider_config``
(invoked here on the TRUE blob, with the loader's flat shape validating
its own assembled blob), and the two sources diverge on stray blob
keys by design (the flat domain stays silently loose until Phase 6).

``KIND_SECTIONS`` maps kind identifiers to their legacy TOML section
names; it is the shared table the Phase 4 migrator consumes so the two
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

# Kinds whose Resource dataclass carries a description field; the
# envelope's metadata.description is injected into the spec for these
# so the shared loaders validate and attach it exactly as for TOML.
_DESCRIPTION_KINDS = {
    "secret",
    "session-template",
    "git-credential",
    "vm-site",
    "apt-source",
    "apt-package",
    "system-install-command",
    "user-install-command",
}


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


def decode_document(doc: Document, issues: list[str]) -> Any:
    """Decode one validated envelope into the kind's Resource instance.

    Spec-level warnings (unknown keys on warn-mode kinds, env hygiene)
    are appended to ``issues`` prefixed with the document location.
    Spec-level errors re-raise as ``ConfigError`` with the same prefix.
    """
    decoder = _DECODERS[doc.kind]
    spec = dict(doc.spec)
    if doc.kind in _DESCRIPTION_KINDS:
        if "description" in spec:
            raise ConfigError(
                f"{doc.where}: description belongs in metadata.description, "
                "not in spec",
            )
        if doc.description is not None:
            spec["description"] = doc.description
    elif doc.description is not None:
        # Warn-and-ignore rather than error: the FRD wants description to
        # become framework-uniform, so a declared description should not
        # block loading a kind that simply hasn't grown the field yet.
        issues.append(
            f"{doc.where}: metadata.description is not yet stored for "
            f"{doc.kind} (the kind's schema has no description field); ignored"
        )

    local_issues: list[str] = []
    try:
        resource = decoder(doc, spec, local_issues)
    except AgentworksError as exc:
        # Catalog loaders raise CatalogError (an ExternalError subclass);
        # from a manifest that is an operator-config mistake, so every
        # spec-level failure re-raises as ConfigError with the document
        # location, per the LLD's error catalog.
        raise ConfigError(f"{doc.where}: {exc}", hint=exc.hint) from exc
    issues.extend(f"{doc.where}: {issue}" for issue in local_issues)
    return resource


def _decode_secret(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_secrets

    result = _load_secrets(
        {"secrets": {doc.name: spec}}, issues, _decls(doc.location)
    )
    return result[doc.name]


def _decode_vm_template(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_vm_templates

    result = _load_vm_templates(
        {"vm_templates": {doc.name: spec}}, issues, _decls(doc.location)
    )
    return result[doc.name]


def _decode_agent_template(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_agent_templates

    result = _load_agent_templates(
        {"agent_templates": {doc.name: spec}}, issues, _decls(doc.location)
    )
    return result[doc.name]


def _decode_workspace_template(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_workspace_templates

    result = _load_workspace_templates(
        {"workspace_templates": {doc.name: spec}}, issues, _decls(doc.location)
    )
    return result[doc.name]


def _decode_session_template(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_session_templates

    result = _load_session_templates(
        {"session_templates": {doc.name: spec}}, issues, _decls(doc.location)
    )
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
    # (azdo's org) nests under spec.provider_config. The YAML shape
    # deliberately diverges from the flat TOML sections here -- the
    # decoder flattens back into the shared loader's shape, so
    # validation stays verbatim-shared with TOML.
    raw_config = spec.pop("provider_config", {})
    if not isinstance(raw_config, dict):
        raise ConfigError("spec.provider_config must be a mapping")
    # The flatten-into-the-loader trick must not let the blob shadow
    # kind-owned surface: without this check, provider_config.token
    # would silently override spec.token, and provider_config.type/
    # provider would silently re-pick the provider.
    reserved = {"type", "provider", "token", "description"} & set(raw_config)
    if reserved:
        names = ", ".join(sorted(reserved))
        raise ConfigError(
            f"spec.provider_config may not contain kind-owned field(s): "
            f"{names}; they belong at the spec top level"
        )
    loader_spec: dict[str, object] = {"type": provider, **raw_config}
    for kind_owned in ("token", "description"):
        if kind_owned in spec:
            loader_spec[kind_owned] = spec.pop(kind_owned)
    if spec:
        extras = ", ".join(sorted(spec))
        raise ConfigError(
            f"unknown git-credential spec field(s): {extras}; "
            "provider-specific configuration (e.g. azdo's org) goes under "
            "spec.provider_config"
        )
    # Capability validation on the TRUE blob (the loader flatten drops
    # keys it doesn't know, so stray blob fields must be caught here,
    # where the error carries this document's file:line). Runs after the
    # spec-shape checks so a misplaced field gets the nesting hint, not
    # a confusing capability complaint. Unknown provider names defer to
    # the framework's miss policy.
    from agentworks.git_credentials import GIT_CREDENTIAL_PROVIDER_REGISTRY

    capability = GIT_CREDENTIAL_PROVIDER_REGISTRY.get(provider)
    if capability is not None:
        capability.validate_config("spec.provider_config", raw_config)
    result = _load_git_credentials(
        {"git_credentials": {doc.name: loader_spec}}, issues, _decls(doc.location)
    )[doc.name]
    # The loader flatten only carries the blob columns the legacy TOML
    # shape knows (org); re-attach the full validated blob so manifest
    # rows keep every capability field (reference derivation at finalize
    # reads it). TOML rows keep the loader's blob -- the flat domain
    # cannot express richer capability config.
    if raw_config:
        import dataclasses

        result = dataclasses.replace(result, provider_config=dict(raw_config))
    return result


def _decode_vm_site(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import validate_name
    from agentworks.vms.sites import VMSiteDecl

    # FRD R2: site names follow the VM-name rules (they appear in
    # hostnames and SSH host aliases).
    validate_name(doc.name)
    platform = spec.pop("platform", None)
    if not isinstance(platform, str) or not platform:
        raise ConfigError(
            "vm-site requires spec.platform (a vm-platform capability name, "
            "e.g. lima, wsl2, azure, proxmox)",
        )
    raw_config = spec.pop("platform_config", {})
    if not isinstance(raw_config, dict):
        raise ConfigError("spec.platform_config must be a mapping")
    # The blob may not shadow kind-owned surface (the git-credential
    # precedent): platform/description in the blob would silently
    # re-pick the capability or override metadata.
    reserved = {"platform", "description"} & set(raw_config)
    if reserved:
        names = ", ".join(sorted(reserved))
        raise ConfigError(
            f"spec.platform_config may not contain kind-owned field(s): "
            f"{names}; they belong at the spec top level"
        )
    description = spec.pop("description", None)
    if spec:
        extras = ", ".join(sorted(spec))
        raise ConfigError(
            f"unknown vm-site spec field(s): {extras}; platform-specific "
            "configuration goes under spec.platform_config"
        )
    # Capability validation on the TRUE blob, with this document's
    # file:line in the error. Unknown platform names are tolerated: the
    # site registers and self-disables ("platform 'x' is not
    # installed") -- a plugin's platform may simply not be here.
    from agentworks.capabilities.vm_platform import VM_PLATFORM_REGISTRY

    # FRD R2: a site named after a known platform must declare that
    # platform -- `vm-site/azure` backed by lima would make every
    # `--site azure` mean something other than it says.
    if doc.name in VM_PLATFORM_REGISTRY and platform != doc.name:
        raise ConfigError(
            f"a vm-site named '{doc.name}' must declare platform "
            f"'{doc.name}' (it shadows a platform name), not '{platform}'"
        )
    capability = VM_PLATFORM_REGISTRY.get(platform)
    if capability is not None:
        capability.validate_config("spec.platform_config", raw_config)
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
        {"admin": {"config": body, "env": env}}, issues, _decls(doc.location)
    )
    assert result is not None  # the key is always present on this path
    return result


def _decode_named_console_template(
    doc: Document, spec: dict[str, object], issues: list[str]
) -> Any:
    from agentworks.config import _load_named_console

    result = _load_named_console(
        {"named_console": spec}, issues, _decls(doc.location)
    )
    assert result is not None  # the key is always present on this path
    return result



def _decode_apt_source(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.catalog import _load_apt_sources

    return _load_apt_sources({doc.name: spec})[doc.name]


def _decode_apt_package(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.catalog import _load_apt_packages

    return _load_apt_packages({doc.name: spec})[doc.name]


def _decode_system_install_command(
    doc: Document, spec: dict[str, object], issues: list[str]
) -> Any:
    from agentworks.catalog import _load_system_commands

    return _load_system_commands({doc.name: spec})[doc.name]


def _decode_user_install_command(
    doc: Document, spec: dict[str, object], issues: list[str]
) -> Any:
    from agentworks.catalog import _load_user_commands

    return _load_user_commands({doc.name: spec})[doc.name]


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
