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
(``spec.platform: {name: lima, placement: {...}}``), which the host row carries
as a ``CapabilityBlock``. The CONTENT of a capability-owned blob is NOT
validated here: its shape check is the finalize ``validate_config`` pass
(R3), against the capability's own declared model.

"""

from __future__ import annotations

from functools import cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError

from agentworks.declared_resource import FRAMEWORK_FIELDS, METADATA_FIELDS, DeclaredResource
from agentworks.errors import ConfigError, StateError
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import RefOwner, config_error_from, extract_references, filled_defaults, located
from agentworks.schema._shape import Collection, shape_of

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.descriptor import CapabilityKindDescriptor
    from agentworks.env import EnvEntry
    from agentworks.manifests.envelope import Document
    from agentworks.schema.reference import ConfigReference

#: The prefix the runtime prelude owns: an operator's value for such a
#: key is silently overridden at command time, so setting one is a
#: mistake worth a warning.
_AGENTWORKS_ENV_PREFIX = "AGENTWORKS_"


def _sample_hint(kind: str) -> str:
    """Where an operator goes to see the shape they got wrong.

    One hint for every kind, built from the kind rather than written per
    kind: the sample surface renders the fields live, so a hand-kept
    per-kind steer would be a second description of the same shape, free to
    drift. It is what pays for the hand-written steers it replaced (the
    vm-site platform enumeration, the git-credential provider list).
    """
    return f"`agw resource sample {kind}` prints this kind's fields"


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
        {descriptor.manifest_section.host_kind: descriptor for descriptor in capability_descriptors()}
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
    model = _model_for(doc.kind)

    owner = RefOwner(kind=doc.kind, name=doc.name)
    _check_declared_name(doc, owner, model)
    _check_declared_description(doc, owner, model)
    # The boundary fill: this is the point that knows the owner, so an
    # omitted owner-templated field is rendered into the payload here and
    # the model validates a complete blob with no context of any kind.
    payload = filled_defaults(model, {**spec, **_metadata_payload(doc)}, owner)
    try:
        resource = model.model_validate(payload)
    except PydanticValidationError as exc:
        raise config_error_from(
            exc,
            model_cls=model,
            owner=owner,
            location=doc.location,
            hint=_sample_hint(doc.kind),
        ) from exc
    issues.extend(located(doc.location, issue) for issue in advisory_issues(resource, doc))
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
    framework = sorted(FRAMEWORK_FIELDS & set(spec))
    if misplaced:
        raise ConfigError(
            located(doc.location, f"{', '.join(misplaced)} belong(s) in metadata, not in spec"),
        )
    if framework:
        raise ConfigError(
            located(doc.location, f"{', '.join(framework)} is set by the framework and cannot be declared"),
        )


def _check_declared_name(doc: Document, owner: RefOwner, model: type[DeclaredResource]) -> None:
    """Apply the kind's name cap, for the kinds that declare one.

    Read off the model rather than dispatched per kind, and applied HERE
    rather than as a validator on the ``name`` field, because only a name
    an operator wrote is checked: auto-declared and synthesized rows carry
    whatever name summoned them and stay tolerant (issue #279). See
    ``DeclaredResource.NAME_MAX_LENGTH``.

    **The character rule rides the cap, so it reaches two kinds of
    thirteen.** ``validate_name`` checks characters and length together
    and is only reached when the kind sets ``NAME_MAX_LENGTH``
    (``secret``, ``vm-site``), so ``vm-template/My Template`` loads,
    lists, and is addressable. That is not obviously wrong: issue #279
    settled that a non-conforming name stays tolerant, and issue #308's
    git-credential warning is the shape that decision takes when a
    non-conforming name has a downstream cost (a derived secret name),
    which is an advisory rather than a refusal. Widening the refusal to
    every kind would reverse both, so it is the operator's call, not a
    cleanup. What was fixed instead is the over-promise: the ``name``
    docstring (rendered into every sample and every ``explain``)
    stated the character rule as a flat rule and now states it as the
    convention it is.
    """
    if model.NAME_MAX_LENGTH is None:
        return
    from agentworks.errors import ValidationError
    from agentworks.naming import validate_name

    try:
        validate_name(doc.name, max_length=model.NAME_MAX_LENGTH)
    except ValidationError as exc:
        raise ConfigError(located(doc.location, f"{owner.display}: {exc}")) from None


def _check_declared_description(doc: Document, owner: RefOwner, model: type[DeclaredResource]) -> None:
    """A kind that REQUIRES a description requires a real one.

    Derived from the model rather than declared per kind: a kind requires
    a description exactly when its row makes the field required, which
    today is ``secret`` alone (it is the operator-facing prompt text, and
    ``secrets/prompt.py`` renders it into "Secret '<name>': <text>").

    Checked HERE and not as a ``NonEmptyStr`` on the field for the reason
    ``NAME_MAX_LENGTH`` is: the framework constructs secret rows with an
    empty description deliberately (``synthesize`` for an auto-declared
    secret, plus four placeholder sites), and the registry's polish pass
    fills the auto-declared ones in afterwards. Only what an operator
    wrote is checked.
    """
    field = model.model_fields.get("description")
    if field is None or not field.is_required() or doc.description != "":
        return
    raise ConfigError(located(doc.location, f"{owner.display}.description: must not be empty"))


def advisory_issues(resource: DeclaredResource, doc: Document) -> list[str]:
    """The load-time warnings this document earns: non-fatal notes an
    operator should act on but that do not stop the config loading.

    Derived, never enumerated. The secret-name check walks every
    ``SecretRef`` edge the models declare, on the row itself and on the
    capability block it hosts, which is what closes issue #311's
    "hand-enumerating the loaders that reference a secret".
    """
    owner = RefOwner(kind=doc.kind, name=doc.name)
    spec = filled_defaults(type(resource), doc.spec, owner)
    refs = [*extract_references(type(resource), spec), *_hosted_capability_references(resource, doc, owner)]
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

    The model is reachable only once the capability's implementation is
    SEATED, and a plugin's impls seat when ``agentworks.plugins`` is
    imported, which no caller of ``load_manifests`` is obliged to have
    done first. Doctor is the surface this advisory exists for and it
    loads manifests before it reaches anything that imports the index, so
    without the import below a non-conforming ``token_secret`` on a
    proxmox site produced no line at all and doctor said "Config is
    valid". That is a REGRESSION rather than a soft edge, because the
    check this replaces was not platform-gated.

    Importing the index here rather than making every caller order itself
    correctly is what keeps the advisory a property of the document. It is
    idempotent and cannot re-enter: building the index only seats impls,
    and the bundled manifests a plugin ships are published later.
    """
    descriptor = _hosting_descriptors().get(doc.kind)
    if descriptor is None:
        return ()
    import agentworks.plugins  # noqa: F401  (imported for the seating side effect)
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
