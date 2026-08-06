"""Spec decode: envelope ``spec`` -> the kind's declared-resource row.

The adapter here knows nothing about any kind. It reads the row model off
``KIND_REGISTRY[doc.kind].model``, merges the envelope's metadata into the
document's ``spec``, validates, and lets the error bridge frame whatever
comes back. Everything a kind used to say about itself now lives on its
model, which is the same class the row IS (see
``agentworks.declared_resource``).

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

from agentworks.declared_resource import METADATA_FIELDS, DeclaredResource
from agentworks.errors import ConfigError, StateError
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import RefOwner, config_error_from, extract_references, validation_context
from agentworks.schema._shape import Collection, shape_of

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

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


def _sample_hint(kind: str) -> str:
    """Where an operator goes to see the shape they got wrong.

    One hint for every kind, built from the kind rather than written per
    kind: the sample surface renders the fields live, and a hand-kept
    per-kind steer is exactly the drift FR13 exists to kill. It is what
    pays for the hand-written steers this step dropped (the vm-site
    platform enumeration, the git-credential provider list).
    """
    return f"`agw resource sample {kind}` prints this kind's fields"


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

Only that one: a naming string, with or without a foldable sibling table.
The migrator refuses to guess at a document that mixes the two shapes, or
at one whose sibling would collide with the tag (which half wins is the
operator's call), so pointing those errors here would send an operator to
a command that leaves their file alone.
"""


def _tagged_rewrite(field: str, name: str, keys: Iterable[str]) -> str:
    """The exact canonical spelling that replaces a legacy sibling pair.

    Built from what the document actually says (the capability's name and
    the config keys it carries) rather than a generic template, so the
    error shows the operator their own resource in the shape it now needs.
    """
    inner = ", ".join([f"name: {name}", *(f"{key}: ..." for key in keys)])
    return f"{field}: {{{inner}}}"


def _reject_legacy_shape(surface: HostSurface, spec: Mapping[str, object], where: str) -> None:
    """The 0.14 sibling pair, refused BY NAME with its rewrite.

    Under the kind spec models this document is two problems the model
    layer has no reason to connect: an unknown ``platform_config`` key and
    a ``platform`` that is not a table. That generic pair is exactly what
    this function exists to beat, so it runs before validation and names
    the fold.

    Two shapes get no printed rewrite, because no honest one exists and a
    rewrite that looks authoritative and is quietly wrong is worse than
    none: a sibling table carrying its OWN ``name`` (the fold would emit
    ``{name: a, name: b}``, which is not valid YAML and hides that two
    keys claim to select the capability), and a sibling that is not a
    table at all (there are no keys to fold, and printing the tag alone
    would discard what the operator wrote). Neither carries the migrate
    hint either, because the migrator refuses both documents too.

    The retired sibling field is refused whatever sits beside it, which is
    the ORPHAN case (a ``platform_config`` alone) and the MIXED one (a
    tagged ``platform`` table beside a stray ``platform_config``). The
    model layer would answer both with a generic unknown key, and the
    operator's next move is the same in both: fold those keys in. No
    migrate hint, because the migrator will not guess which half of a
    mixed document wins and has nothing to fold in the orphan one.

    This is the one compatibility surface the model swap leaves behind,
    and it goes together with ``HostSurface.config_field``, whose only
    remaining job is to let this name the retired field. Delete both when
    the shape is far enough in the past that a generic unknown key is a
    good enough answer.
    """
    field, config_field = surface.naming_field, surface.config_field
    value = spec.get(field)
    if not isinstance(value, str):
        if config_field in spec:
            raise ConfigError(
                f"{where}: spec.{config_field} is not a supported YAML field; "
                f"fold its keys into the spec.{field} tagged table"
            )
        return
    name = value or "<capability>"
    head = f"{where}: spec.{field} names the capability as a string, which is no longer supported"
    if config_field not in spec:
        raise ConfigError(
            f"{head}; write one tagged table instead: {_tagged_rewrite(field, name, ())}", hint=MIGRATE_HINT
        )
    sibling = spec[config_field]
    if not isinstance(sibling, dict):
        raise ConfigError(
            f"{head}, and spec.{config_field} is {sibling!r} rather than a table, so there are no keys to fold; "
            f"write spec.{field} as one tagged table and put that value where it belongs, or remove it"
        )
    if "name" in sibling:
        raise ConfigError(
            f"{head}, and spec.{config_field} carries its own 'name' ({sibling['name']!r}), so which one "
            f"selects the capability is your call; merge them by hand into one spec.{field} table"
        )
    raise ConfigError(
        f"{head}; write one tagged table instead: {_tagged_rewrite(field, name, sibling)}", hint=MIGRATE_HINT
    )


def decode_document(doc: Document, issues: list[str]) -> Any:
    """Decode one validated envelope into the kind's declared-resource row.

    The spec is validated against the kind's model, which is the row class
    itself; the error bridge frames every problem with the document's
    ``file:line`` and the owner's ``kind/name``. Advisory warnings are
    appended to ``issues``, located the same way.
    """
    spec = dict(doc.spec)
    _reject_spec_metadata(doc, spec)
    descriptor = _hosting_descriptors().get(doc.kind)
    if descriptor is not None and descriptor.manifest_section is not None:
        _reject_legacy_shape(descriptor.manifest_section, spec, doc.where)
    model = _model_for(doc.kind)

    owner = RefOwner(kind=doc.kind, name=doc.name)
    _check_declared_name(doc, owner, model)
    payload = {**spec, **_metadata_payload(doc)}
    try:
        resource = model.model_validate(payload, context=validation_context(owner))
    except PydanticValidationError as exc:
        raise config_error_from(
            exc,
            model_cls=model,
            owner=owner,
            location=doc.location,
            hint=_sample_hint(doc.kind),
        ) from exc
    issues.extend(f"{doc.where}: {issue}" for issue in advisory_issues(resource, doc))
    return resource


def _model_for(kind: str) -> type[DeclaredResource]:
    """The kind's spec model, which is its declared-resource row class.

    Read off the kind strategy rather than out of a table here, so the
    switchboard is derived: a new declarable kind cannot be added without
    one, and nothing in the manifest layer enumerates the kinds.
    ``tests/manifests/test_kind_models.py`` pins the split (every
    declarable kind declares one, no capability kind does).
    """
    model = getattr(KIND_REGISTRY[kind], "model", None)
    if not (isinstance(model, type) and issubclass(model, DeclaredResource)):
        raise StateError(f"the {kind} kind declares no spec model, so a manifest document has nothing to validate")
    return model


def _metadata_payload(doc: Document) -> dict[str, object]:
    """The envelope's metadata, as row fields.

    ``description`` is injected only when the document declares one, so a
    kind that requires it reports a missing field rather than a null.
    """
    payload: dict[str, object] = {"name": doc.name, "declared_at": doc.location}
    if doc.description is not None:
        payload["description"] = doc.description
    if doc.expires is not None:
        payload["expires"] = doc.expires
    return payload


def _reject_spec_metadata(doc: Document, spec: Mapping[str, object]) -> None:
    """Refuse a non-spec field written inside ``spec``.

    ``extra="forbid"`` closes the spec surface against keys the row does
    not have, but the envelope and framework fields ARE fields of the row,
    so ``spec.name`` would be accepted and would silently override the
    envelope. Both sets are derived from the row base, so neither can fall
    behind a new field.

    The two get different answers because they have different remedies.
    An envelope field belongs somewhere: ``metadata``. A framework field
    belongs nowhere, and answering ``spec.origin`` with "it belongs in
    metadata" would send an operator to write ``metadata.origin``, which
    the envelope then refuses as an unknown metadata key.
    """
    misplaced = sorted(METADATA_FIELDS & set(spec))
    framework = sorted((_ROW_METADATA_FIELDS - METADATA_FIELDS) & set(spec))
    if misplaced:
        raise ConfigError(
            f"{doc.where}: {', '.join(misplaced)} belong(s) in metadata, not in spec",
        )
    if framework:
        raise ConfigError(
            f"{doc.where}: {', '.join(framework)} is set by the framework and cannot be declared",
        )


def _check_declared_name(doc: Document, owner: RefOwner, model: type[DeclaredResource]) -> None:
    """Apply the kind's name cap, for the kinds that declare one.

    Read off the model rather than dispatched per kind, and applied HERE
    rather than as a validator on the ``name`` field, because only a name
    an operator wrote is checked: auto-declared and synthesized rows carry
    whatever name summoned them and stay tolerant (issue #279). See
    ``DeclaredResource.NAME_MAX_LENGTH``.
    """
    if model.NAME_MAX_LENGTH is None:
        return
    from agentworks.errors import ValidationError
    from agentworks.naming import validate_name

    try:
        validate_name(doc.name, max_length=model.NAME_MAX_LENGTH)
    except ValidationError as exc:
        raise ConfigError(f"{doc.where}: {owner.display}: {exc}") from None


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
    return capability_config_references(kind=descriptor.kind, config=block.tagged, owner=owner)


def _conforming_secret(name: str) -> bool:
    from agentworks.errors import ValidationError
    from agentworks.naming import MAX_SECRET_NAME_LENGTH, validate_name

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
    from agentworks.naming import MAX_SECRET_NAME_LENGTH

    return (
        f"{owner.display}: secret name {ref.name!r} for {ref.usage} does not follow the secret naming "
        f"rules (lowercase alphanumeric with hyphens or underscores, starting and ending with a letter "
        f"or digit, at most {MAX_SECRET_NAME_LENGTH} characters). It still resolves as declared; rename "
        f"it to conform."
    )
