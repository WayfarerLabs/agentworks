"""Spec decode: envelope ``spec`` -> the kind's declared-resource row.

The adapter here knows nothing about any kind. It reads the row model off
``KIND_REGISTRY[doc.kind].model``, merges the envelope's metadata into the
document's ``spec``, validates, and lets the error bridge frame whatever
comes back. Everything a kind used to say about itself now lives on its
model, which is the same class the row IS (see
``agentworks.declared_resource``).

INTERIM, for the length of step 2.5 only: kinds whose model has not
absorbed its decoder yet still route through ``_DECODERS`` below. That
fork is what lets the swap land one kind at a time with the suite green
after each; it goes when the last kind moves, and ``_model_for`` is the
one place that decides.

**Advisory checks are derived from a declared type or a declared marker,
never from an enumeration of kinds.** ``ManifestSet.issues`` is the
load-time warning channel, and a check wired per kind is a check the sixth
kind of that shape will silently lack. So the non-conforming-secret-name
warning walks the ``SecretRef`` edges the model declares
(:func:`advisory_issues`), rather than being called at each site that
happens to name a secret.

A capability is named by ONE tagged table on its host's naming field
(``spec.platform: {name: lima, vm_host: ...}``), which the host row carries
as a ``CapabilityBlock``. The CONTENT of a capability-owned blob is NOT
validated here: its shape check is the finalize ``validate_config`` pass
(R3), against the capability's own declared model.

``KIND_SECTIONS`` maps kind identifiers to their legacy TOML section
names; it is the shared table the manifest migrator consumes so the two
sides cannot disagree about what maps to what.
"""

from __future__ import annotations

from functools import cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError

from agentworks.declared_resource import DeclaredResource
from agentworks.errors import AgentworksError, ConfigError
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import RefOwner, config_error_from, extract_references, validation_context
from agentworks.schema._shape import Collection, shape_of

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentworks.capabilities.descriptor import CapabilityKindDescriptor, HostSurface
    from agentworks.env import EnvEntry
    from agentworks.manifests.envelope import Document
    from agentworks.schema.reference import ConfigReference

#: The fields a row carries as METADATA rather than as spec surface. They
#: are real fields of the model, so a document writing one inside ``spec``
#: would be accepted and would silently override the envelope; derived from
#: the base rather than listed, so a fourth metadata field cannot be
#: rejected by one layer and accepted by the other.
_ROW_METADATA_FIELDS = frozenset(DeclaredResource.model_fields)

#: The prefix the runtime prelude owns: an operator's value for such a
#: key is silently overridden at command time, so setting one is a
#: mistake worth a warning.
_AGENTWORKS_ENV_PREFIX = "AGENTWORKS_"

#: Where an operator goes to see the shape they got wrong. One hint for
#: every kind, because the sample surface renders the fields live and a
#: hand-kept per-kind steer is exactly the drift FR13 exists to kill.
_SAMPLE_HINT = "see `agw resource sample <kind>` for this kind's fields"


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


@cache
def _hosting_descriptors() -> Mapping[str, CapabilityKindDescriptor]:
    """Declarable kind -> the descriptor of the capability kind its spec
    selects.

    The whole descriptor rather than its ``manifest_section`` alone,
    because a caller needs both halves: the field names to read
    (``manifest_section``) and the capability kind to validate or extract
    against (``kind``).

    Derived from the capability-kind descriptor table, which is where a
    kind records how it is selected inside its host's spec. Three of the
    four kinds have a host surface (``vm-site`` hosts vm-platform,
    ``git-credential`` hosts git-credential-provider, ``session-template``
    hosts harness-integration); ``secret-backend`` has none, because the
    per-secret ``backend_mappings`` map key already names the capability.
    Membership IS the dispatch: a declarable kind in this map gets the
    capability fold, and one absent from it names no capability.

    An accessor rather than a module-level constant, for UNIFORMITY with the
    derived sites where laziness is forced, not because a cycle threatens
    here: none of the four contributing ``kinds.py`` modules loads anything
    under ``agentworks.manifests``, and ``agentworks.manifests.__init__``
    already loads all four. It does keep the load boundary where the
    descriptor module puts it, collecting the table on first use rather than
    at import of whoever imports this.

    Host kinds are unique across the table (a declarable kind hosts at most
    one capability kind), so keying by ``host_kind`` loses nothing. The
    descriptor-table tests assert that, because if two records ever claimed
    the same host, one of them would silently vanish here along with its
    fold.
    """
    from agentworks.capabilities.descriptor import capability_descriptors

    return MappingProxyType(
        {
            descriptor.manifest_section.host_kind: descriptor
            for descriptor in capability_descriptors()
            if descriptor.manifest_section is not None
        }
    )


MIGRATE_HINT = "`agw resource migrate --all` rewrites your manifests in place."
"""Remediation for the legacy shape the migrator can MECHANICALLY fold.

Only that one: a naming string, with or without its sibling table. The
migrator deliberately refuses to guess at a document that mixes the two
shapes (which half wins is the operator's call), so pointing that error
here would send them to a command that leaves their file alone.
"""


def _tagged_rewrite(field: str, name: str, config: object) -> str:
    """The exact canonical spelling that replaces a legacy sibling pair.

    Built from what the document actually says (the capability's name and
    the config keys it carries) rather than a generic template, so the
    error shows the operator their own resource in the shape it now needs.
    """
    keys = list(config) if isinstance(config, dict) else []
    inner = ", ".join([f"name: {name}", *(f"{key}: ..." for key in keys)])
    return f"{field}: {{{inner}}}"


def _fold_capability_table(surface: HostSurface, spec: dict[str, object]) -> None:
    """Fold the tagged capability table into the internal sibling pair.

    The manifest shape is one tagged table on the naming field
    (``platform: {name: lima, vm_host: ...}``): ``name`` selects the
    capability and the remaining keys are its config. The decoders and the
    shared TOML loaders underneath them consume a naming STRING plus a
    ``*_config`` mapping, so this splits the table back into that internal
    pair. Every host surface folds the same way; the field names come from
    the kind's descriptor record.

    A host that names no capability at all passes through untouched: a
    session template legitimately inherits its selector or stays the
    default login shell, and the kinds that REQUIRE one raise their own
    required-field error in their decoder, where the kind's vocabulary is.

    Every other spelling is a hard error, including the legacy sibling
    pair (a naming string beside a ``*_config`` table) that decode accepted
    with a deprecation warning through 0.14.
    """
    field, config_field = surface.naming_field, surface.config_field
    if field not in spec and config_field not in spec:
        return
    value = spec.pop(field, None)
    if isinstance(value, str):
        rewrite = _tagged_rewrite(field, value or "<capability>", spec.get(config_field))
        raise ConfigError(
            f"spec.{field} names the capability as a string, which is no longer "
            f"supported; write one tagged table instead: {rewrite}",
            hint=MIGRATE_HINT,
        )
    if config_field in spec:
        # No migrate hint: this is either a tagged table beside a stray
        # sibling (which half wins is a judgement call, so the migrator
        # refuses to guess) or an ownerless blob. Both are hand fixes.
        raise ConfigError(
            f"spec.{config_field} is not a supported YAML field; fold its keys into the spec.{field} tagged table"
        )
    if not isinstance(value, dict):
        raise ConfigError(f"spec.{field} must be a tagged table with a string 'name' key")
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"spec.{field} (table form) requires a 'name' key naming the capability")
    config = {key: item for key, item in value.items() if key != "name"}
    spec[field] = name
    if config:
        spec[config_field] = config


def decode_document(doc: Document, issues: list[str]) -> Any:
    """Decode one validated envelope into the kind's declared-resource row.

    The spec is validated against the kind's model, which is the row class
    itself; the error bridge frames every problem with the document's
    ``file:line`` and the owner's ``kind/name``. Advisory warnings are
    appended to ``issues``, located the same way.
    """
    spec = dict(doc.spec)
    _reject_spec_metadata(doc, spec)
    model = _model_for(doc.kind)
    if model is None:
        return _legacy_decode(doc, spec, issues)

    owner = RefOwner(kind=doc.kind, name=doc.name)
    payload = {**spec, **_metadata_payload(doc)}
    try:
        resource = model.model_validate(payload, context=validation_context(owner))
    except PydanticValidationError as exc:
        raise config_error_from(
            exc,
            model_cls=model,
            owner=owner,
            location=doc.location,
            hint=_SAMPLE_HINT,
        ) from exc
    issues.extend(f"{doc.where}: {issue}" for issue in advisory_issues(resource, doc))
    return resource


def _model_for(kind: str) -> type[DeclaredResource] | None:
    """The kind's row model, or ``None`` while it is still decoded by
    hand.

    INTERIM: the ``None`` answer, ``_legacy_decode``, and the
    ``_DECODERS`` table go together when the last kind gains a model.
    """
    model = getattr(KIND_REGISTRY[kind], "model", None)
    return model if isinstance(model, type) and issubclass(model, DeclaredResource) else None


def _metadata_payload(doc: Document) -> dict[str, object]:
    """The envelope's metadata, as row fields.

    ``description`` is injected only when the document declares one, so a
    kind that requires it reports a missing field rather than a null.
    """
    payload: dict[str, object] = {"name": doc.name, "declared_at": doc.location}
    if doc.description is not None:
        payload["description"] = doc.description
    return payload


def _reject_spec_metadata(doc: Document, spec: Mapping[str, object]) -> None:
    """Refuse a metadata field written inside ``spec``.

    ``extra="forbid"`` closes the spec surface against keys the row does
    not have, but the metadata fields ARE fields of the row, so
    ``spec.name`` would be accepted and would silently override the
    envelope. Derived from the base, so it cannot fall behind a new
    metadata field.
    """
    reserved = sorted(_ROW_METADATA_FIELDS & set(spec))
    if reserved:
        raise ConfigError(
            f"{doc.where}: {', '.join(reserved)} belong(s) in metadata, not in spec",
        )


def advisory_issues(resource: DeclaredResource, doc: Document) -> list[str]:
    """The load-time warnings this document earns: non-fatal notes an
    operator should act on but that do not stop the config loading.

    Derived, never enumerated. The secret-name check walks every
    ``SecretRef`` edge the models declare, on the row itself and on the
    capability block it hosts, which is what closes issue #311's
    "hand-enumerating the loaders that reference a secret".
    """
    owner = RefOwner(kind=doc.kind, name=doc.name)
    refs = [*extract_references(type(resource), doc.spec, owner), *_hosted_capability_references(resource, doc, owner)]
    return [
        _nonconforming_secret(owner, ref) for ref in refs if ref.kind == "secret" and not _conforming_secret(ref.name)
    ] + _env_hygiene_issues(owner, resource)


def _env_hygiene_issues(owner: RefOwner, resource: DeclaredResource) -> list[str]:
    """Two warnings an env table earns, on every kind that has one.

    Found by ANNOTATION (a mapping of ``EnvEntry``), not by a list of
    env-bearing kinds, which is the list the sixth such kind would not be
    on. Neither check can live on the model: a model validator has no
    channel but an exception, and neither of these is fatal.
    """
    from agentworks.env.entry import EnvEntry

    issues: list[str] = []
    for name, field in type(resource).model_fields.items():
        shape = shape_of(field)
        if shape.collection is not Collection.MAPPING or shape.item_model is not EnvEntry:
            continue
        table: Mapping[str, EnvEntry] = getattr(resource, name, None) or {}
        for key, entry in table.items():
            issues.extend(_env_entry_issues(owner, name, key, entry))
    return issues


def _env_entry_issues(owner: RefOwner, field: str, key: str, entry: EnvEntry) -> list[str]:
    issues: list[str] = []
    if key.startswith(_AGENTWORKS_ENV_PREFIX):
        issues.append(
            f"{owner.display}.{field} sets agentworks-managed identity variable "
            f"{key!r}; identity values win at the runtime prelude, so your value "
            "will be ignored at command time. Remove the entry."
        )
    if entry.value is not None and ("\n" in entry.value or "\r" in entry.value):
        # ADR 0014: a newline would corrupt the SSH ``-o SetEnv=KEY=VALUE``
        # argument shape. The resolve loop applies the same check
        # defensively to secret-resolved values.
        issues.append(
            f"{owner.display}.{field}.{key}: value contains a newline; SSH SetEnv "
            "cannot transport it cleanly. Strip the newline at the source."
        )
    return issues


def _hosted_capability_references(
    resource: DeclaredResource,
    doc: Document,
    owner: RefOwner,
) -> tuple[ConfigReference, ...]:
    """The references the capability block this kind hosts implies, read
    off that capability's own declared model.

    One honest soft edge: a capability seated by a PLUGIN has not been
    imported when manifests load (``bootstrap.build_registry`` seats
    plugins after ``load_manifests``), so its blob contributes nothing
    here. That is a missed advisory line, never a wrong answer, and the
    finalize pass still checks the blob's shape.
    """
    descriptor = _hosting_descriptors().get(doc.kind)
    if descriptor is None or descriptor.manifest_section is None:
        return ()
    from agentworks.capabilities.config import capability_config_references
    from agentworks.schema import CapabilityBlock

    block = getattr(resource, descriptor.manifest_section.naming_field, None)
    if not isinstance(block, CapabilityBlock):
        return ()
    return capability_config_references(
        kind=descriptor.kind,
        name=block.name,
        blob=block.config,
        owner=owner,
    )


def _conforming_secret(name: str) -> bool:
    from agentworks.config.validation import MAX_SECRET_NAME_LENGTH, validate_name
    from agentworks.errors import ValidationError

    try:
        validate_name(name, max_length=MAX_SECRET_NAME_LENGTH)
    except ValidationError:
        return False
    return True


def _nonconforming_secret(owner: RefOwner, ref: ConfigReference) -> str:
    """The warning text for a secret NAME an operator supplied that does
    not follow the naming rules.

    A warning rather than an error, deliberately: a non-conforming
    reference still declares and resolves exactly as before, so this
    unifies the guarantee at the operator boundary (issues #279, #308)
    without breaking a config that already loads.
    """
    from agentworks.config.validation import MAX_SECRET_NAME_LENGTH

    return (
        f"{owner.display}: secret name {ref.name!r} for {ref.usage} does not follow the secret naming "
        f"rules (lowercase alphanumeric with hyphens or underscores, starting and ending with a letter "
        f"or digit, at most {MAX_SECRET_NAME_LENGTH} characters). It still resolves as declared; rename "
        f"it to conform."
    )


def _legacy_decode(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    """The pre-model decode path, for the kinds whose model has not
    absorbed its decoder yet. INTERIM; see :func:`_model_for`."""
    decoder = _DECODERS[doc.kind]
    if doc.description is not None:
        spec["description"] = doc.description

    local_issues: list[str] = []
    try:
        descriptor = _hosting_descriptors().get(doc.kind)
        if descriptor is not None and descriptor.manifest_section is not None:
            _fold_capability_table(descriptor.manifest_section, spec)
        resource = decoder(doc, spec, local_issues)
    except AgentworksError as exc:
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

    packages = _require_string_list(spec, "mise_packages", f"agent_templates.{name}")
    lockfile_value = spec.get("mise_lockfile")
    if lockfile_value is not None and not isinstance(lockfile_value, str):
        raise ConfigError(f"agent_templates.{name}.mise_lockfile must be a string")
    lockfile = lockfile_value
    install_before_value = spec.get("mise_install_before", "7d")
    if not isinstance(install_before_value, str):
        raise ConfigError(f"agent_templates.{name}.mise_install_before must be a string")
    install_before = install_before_value
    from agentworks.config.validation import validate_mise_settings

    validate_mise_settings(packages, lockfile, install_before, context=f"agent_templates.{name}")

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
        mise_packages=packages if "mise_packages" in spec else None,
        mise_lockfile=lockfile,
        mise_allow_unlocked=(bool(spec["mise_allow_unlocked"]) if "mise_allow_unlocked" in spec else None),
        mise_install_before=(install_before if "mise_install_before" in spec else None),
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


_SESSION_TEMPLATE_KEYS = {
    "inherits",
    "description",
    "harness_integration",
    "harness_integration_config",
    "env",
}


def _decode_session_template(doc: Document, spec: dict[str, Any], issues: list[str]) -> Any:
    from agentworks.config.loaders_core import _parse_env_table, _raise_unexpected_keys
    from agentworks.sessions.template import SessionTemplate

    # The capability fold (tagged harness_integration table -> the internal
    # ``harness_integration`` name plus ``harness_integration_config`` blob)
    # already ran in ``decode_document`` before this decoder, so the spec here
    # carries the canonical internal pair: a non-empty string selector, and a
    # mapping blob only where there is a selector to own it. Shell
    # configuration lives only under harness_integration_config. This kind is
    # strict at its own boundary so misspelled or removed fields do not degrade
    # into warn-mode handling.
    name = doc.name
    _raise_unexpected_keys(spec, _SESSION_TEMPLATE_KEYS, f"session_templates.{name}")
    harness_integration = spec.get("harness_integration")
    if harness_integration is not None and not isinstance(harness_integration, str):
        raise ConfigError(f"session_templates.{name}.harness_integration must be a string")
    raw_config = spec.get("harness_integration_config")
    config_blob = dict(raw_config) if isinstance(raw_config, dict) else None
    env: dict[str, EnvEntry] | None = None
    if "env" in spec:
        env = _parse_env_table(spec["env"], context=f"session_templates.{name}", issues=issues)
    return SessionTemplate(
        name=name,
        inherits=list(spec.get("inherits", [])),
        description=str(spec["description"]) if "description" in spec else None,
        harness_integration=harness_integration,
        harness_integration_config=config_blob,
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
    # By this point ``_fold_capability_table`` has already split the tagged
    # ``spec.provider: {name: ..., ...}`` table into the internal pair (a
    # ``provider`` string plus a ``provider_config`` mapping), so this decoder
    # always sees the normalized form: absent, or a non-empty string beside a
    # mapping. Only absence is left to check here.
    provider = spec.pop("provider", None)
    if not isinstance(provider, str) or not provider:
        raise ConfigError(
            "git-credential requires spec.provider (github or azdo)",
        )
    # Provider-owned configuration (azdo's org, the token secret name) rides
    # the provider table operators write and lands in this mapping.
    raw_config = spec.pop("provider_config", {})
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
    # ``_fold_capability_table`` has already split the tagged
    # ``spec.platform: {name: ..., ...}`` table into the internal pair, so the
    # platform arrives absent or as a non-empty string beside a mapping. Only
    # absence is left to check here.
    platform = spec.pop("platform", None)
    if not isinstance(platform, str) or not platform:
        raise ConfigError(
            "vm-site requires spec.platform (a vm-platform capability name, "
            "e.g. lima, wsl2, azure-vm, aws-ec2, proxmox)",
        )
    raw_config = spec.pop("platform_config", {})
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
        _warn_nonconforming_secret_name(token_secret, location="token_secret (platform config)", issues=issues)
    # The blob may not shadow kind-owned surface (the git-credential
    # precedent): platform/description in the blob would silently
    # re-pick the capability or override metadata.
    reserved = {"platform", "description"} & set(raw_config)
    if reserved:
        names = ", ".join(sorted(reserved))
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
    # ``validate`` pass (VMSiteDecl.validate), not here: the core validates
    # it against the platform's declared model, decoupled from decode (R3).
    # The shadow check below is kind-owned decode structure and stays at
    # load. An unknown platform NAME is not decode's business either: the
    # site emits its platform edge unconditionally and the dangling edge is
    # a hard finalize miss (R9.2). (It is not tolerated-and-self-disabled;
    # that was the pre-registry-readiness behavior, and this comment
    # described it long after it stopped being true.)
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


_DECODERS: dict[str, Callable[[Document, dict[str, Any], list[str]], Any]] = {
    "secret": _decode_secret,
    "vm-template": _decode_vm_template,
    "agent-template": _decode_agent_template,
    "session-template": _decode_session_template,
    "git-credential": _decode_git_credential,
    "vm-site": _decode_vm_site,
}
