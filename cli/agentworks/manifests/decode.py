"""Spec decode: envelope ``spec`` -> the kind's Resource dataclass.

The decoders do NOT reimplement field validation. Each one reassembles
the shape the corresponding TOML loader consumes and calls that loader
with a fixed-location ``decls`` shim, so every type check, enum, env
entry rule, and unknown-key warning is shared verbatim between the TOML
and manifest sources. When the TOML resource surface is deleted at the
cutover, these loaders become manifest-only and can be renamed in place.

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


# Kind identifier -> legacy TOML section name (the migrator's table).
KIND_SECTIONS: dict[str, str] = {
    "secret": "secrets",
    "vm-template": "vm_templates",
    "agent-template": "agent_templates",
    "workspace-template": "workspace_templates",
    "session-template": "session_templates",
    "git-credential": "git_credentials",
    "admin-template": "admin",
    "named-console-template": "named_console",
    # secret-backend: capability descriptor, not declarable (no decoder);
    # listed for the migrator's [secret_backends.*] drop handling only.
    "secret-backend": "secret_backends",
    "apt-source": "apt_sources",
    "apt-package": "apt_packages",
    "system-install-command": "system_install_commands",
    "user-install-command": "user_install_commands",
}

# Kinds whose Resource dataclass carries a description field; the
# envelope's metadata.description is injected into the spec for these
# so the shared loaders validate and attach it exactly as for TOML.
_DESCRIPTION_KINDS = {
    "secret",
    "session-template",
    "git-credential",
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
    result = _load_git_credentials(
        {"git_credentials": {doc.name: loader_spec}}, issues, _decls(doc.location)
    )
    return result[doc.name]


def _decode_admin_template(doc: Document, spec: dict[str, object], issues: list[str]) -> Any:
    from agentworks.config import _load_admin_config

    body = dict(spec)
    env = body.pop("env", {})
    return _load_admin_config(
        {"admin": {"config": body, "env": env}}, issues, _decls(doc.location)
    )


def _decode_named_console_template(
    doc: Document, spec: dict[str, object], issues: list[str]
) -> Any:
    from agentworks.config import _load_named_console

    return _load_named_console(
        {"named_console": spec}, issues, _decls(doc.location)
    )



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
    "admin-template": _decode_admin_template,
    "named-console-template": _decode_named_console_template,
    "apt-source": _decode_apt_source,
    "apt-package": _decode_apt_package,
    "system-install-command": _decode_system_install_command,
    "user-install-command": _decode_user_install_command,
}
