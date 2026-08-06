"""Emitted JSON Schema: the documents, and what an editor makes of them.

The reference implementation (``jsonschema``, a dev-only dependency) is
the oracle throughout, and that is the point of the file. Emission's
failure mode is a schema that is subtly WRONG, which an editor turns into
a confident red underline on valid config, and assertions hand-written by
the author of the emitter would encode the same misunderstanding. So the
structural claims are checked against the 2020-12 metaschema and the
behavioral ones by validating real documents.

The soundness contract these tests exist to defend (see
``agentworks/manifests/emit.py``): everything the schema rejects, the
loader rejects too. Nothing here pins the converse, which is false by
design.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import pytest
import yaml
from jsonschema import Draft202012Validator

from agentworks.declared_resource import METADATA_FIELDS
from agentworks.manifests.emit import (
    ENVELOPE_SCHEMA_FILENAME,
    SCHEMA_DIRNAME,
    document_schema,
    emittable_kinds,
    envelope_schema,
    modeline,
    schema_filename,
    schema_set,
    write_schema_set,
)
from agentworks.manifests.envelope import _ENVELOPE_KEYS, API_VERSION
from agentworks.manifests.loader import load_manifests
from agentworks.manifests.samples import SAMPLE_KINDS, sample_text
from agentworks.plugins import Plugin, seated_plugin
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import AgwModel
from tests.plugins._fixtures import ConformingVMPlatform

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class _EditorLoader(yaml.SafeLoader):
    """A YAML load that sees what a SCHEMA-AWARE EDITOR sees.

    yaml-language-server reads the document's syntax tree under YAML 1.2's
    core schema, which has no implicit timestamp type, so ``2026-01-01``
    reaches its validator as a string. pyyaml's safe loader resolves it to
    a ``datetime.date``, which no JSON Schema ``type`` matches, and a test
    validating THAT would be testing a document no editor ever holds.
    """


_EditorLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _uncomment(text: str) -> str:
    """The sample surface's documented uncomment rule: strip one leading
    ``#`` per line."""
    return "\n".join(line[1:] if line.startswith("#") else line for line in text.splitlines()) + "\n"


def _documents(text: str) -> list[dict[str, Any]]:
    return [doc for doc in yaml.load_all(text, Loader=_EditorLoader) if isinstance(doc, dict)]


def _errors(schema: dict[str, Any], document: object) -> list[str]:
    return [error.message for error in Draft202012Validator(schema).iter_errors(document)]


def _a_document(kind: str, spec: object, **metadata: object) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "kind": kind,
        "metadata": {"name": "sample", **metadata},
        "spec": spec,
    }


# -- The set ---------------------------------------------------------------


def test_every_declarable_kind_is_emittable_and_no_capability_kind_is() -> None:
    """A kind has a schema exactly when a document of it can exist, so the
    emittable set is derived from the registry's per-kind category rather
    than listed. Same derivation the sample surface uses, so the two
    surfaces cannot come to describe different kinds."""
    declarable = {name for name, handler in KIND_REGISTRY.items() if handler.category == "declarable"}
    assert set(emittable_kinds()) == declarable
    assert set(emittable_kinds()) == set(SAMPLE_KINDS)
    assert sorted(schema_set()) == sorted(
        [ENVELOPE_SCHEMA_FILENAME, *(schema_filename(kind) for kind in emittable_kinds())]
    )


def test_every_emitted_schema_meta_validates_as_2020_12() -> None:
    """The reference implementation checking our output against the
    2020-12 metaschema: this is what catches a malformed ``$defs`` graph
    or a ``$ref`` pointing at nothing, neither of which any assertion we
    could write by hand would notice."""
    for filename, schema in schema_set().items():
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema", filename
        Draft202012Validator.check_schema(schema)


def test_unknown_and_capability_kinds_are_clean_domain_errors() -> None:
    from agentworks.errors import ValidationError

    with pytest.raises(ValidationError, match="unknown kind"):
        document_schema("nope")
    with pytest.raises(ValidationError, match="capability kind"):
        document_schema("vm-platform")


# -- The document envelope -------------------------------------------------


def test_the_document_schema_states_exactly_the_envelope_keys() -> None:
    """The one fact the emission model OWNS rather than reads, pinned
    against the hand-rolled validator that enforces it at load. A fifth
    envelope key accepted by one layer and missing from the other is
    exactly the drift this asserts away."""
    schema = document_schema("secret")
    assert set(schema["properties"]) == _ENVELOPE_KEYS
    assert set(schema["required"]) == _ENVELOPE_KEYS
    assert schema["additionalProperties"] is False


def test_metadata_carries_the_envelope_metadata_keys_and_nothing_else() -> None:
    schema = document_schema("vm-template")
    metadata = schema["$defs"]["VmTemplateMetadata"]
    assert set(metadata["properties"]) == METADATA_FIELDS
    assert metadata["additionalProperties"] is False


def test_metadata_follows_the_KIND_not_the_shared_base() -> None:
    """``secret`` makes ``description`` required and ``admin-template``
    defaults ``name``. Both are re-declarations on the kind's own row, and
    reading metadata off the row rather than off ``EnvelopeMetadata`` is
    what makes them show up here."""
    secret = document_schema("secret")["$defs"]["SecretMetadata"]
    assert sorted(secret["required"]) == ["description", "name"]

    admin = document_schema("admin-template")["$defs"]["AdminTemplateMetadata"]
    assert admin["properties"]["name"]["default"] == "default"
    assert "required" not in admin


def test_a_name_cap_the_decoder_applies_reaches_the_schema() -> None:
    """``NAME_MAX_LENGTH`` is applied by decode to exactly the names a
    manifest carries, so stating it is faithful. It is also one derived
    integer, which is why it is emitted where the character rule is
    not."""
    from agentworks.naming import MAX_SECRET_NAME_LENGTH

    secret = document_schema("secret")["$defs"]["SecretMetadata"]
    assert secret["properties"]["name"]["maxLength"] == MAX_SECRET_NAME_LENGTH
    template = document_schema("vm-template")["$defs"]["VmTemplateMetadata"]
    assert "maxLength" not in template["properties"]["name"]


def test_the_envelope_schema_discriminates_on_kind() -> None:
    schema = envelope_schema()
    assert schema["discriminator"]["propertyName"] == "kind"
    assert set(schema["discriminator"]["mapping"]) == set(emittable_kinds())
    assert len(schema["oneOf"]) == len(emittable_kinds())


# -- The capability splice -------------------------------------------------


def test_the_capability_union_replaces_the_open_block_at_the_host_field() -> None:
    """A row carries its capability as an open ``CapabilityBlock``, which
    as a schema says only "some table with a name". Emission replaces it
    with the union over what is registered, keeping the row's authored
    description."""
    schema = document_schema("vm-site")
    platform = schema["$defs"]["VmSiteSpec"]["properties"]["platform"]
    assert "vm-platform backing this site" in platform["description"]

    union = schema["$defs"][platform["$ref"].rsplit("/", 1)[-1]]
    assert union["discriminator"]["propertyName"] == "name"
    assert set(union["discriminator"]["mapping"]) == _platform_names()
    # The open block is gone entirely, not merely shadowed.
    assert "CapabilityBlock" not in schema["$defs"]


def _platform_names() -> set[str]:
    from agentworks.capabilities.descriptor import descriptor_for

    return set(descriptor_for("vm-platform").registry())


def test_a_typo_inside_a_capability_config_is_a_schema_error() -> None:
    """What the splice buys: without it the block is ``extra="allow"`` and
    an editor would accept anything. The finalize pass catches this
    either way; the schema is what catches it while the operator types."""
    schema = document_schema("vm-site")
    good = _a_document("vm-site", {"platform": {"name": "lima", "vm_host": "me@box"}})
    assert _errors(schema, good) == []
    typo = _a_document("vm-site", {"platform": {"name": "lima", "vmhost": "me@box"}})
    assert _errors(schema, typo)


def test_an_unknown_spec_key_is_a_schema_error() -> None:
    """FR12's closed world reaches the editor: the models are
    ``extra="forbid"`` and emission carries that through."""
    schema = document_schema("apt-package")
    assert _errors(schema, _a_document("apt-package", {"nonsense": 1}))


class OnlyConfig(AgwModel):
    """The only arm a one-arm union has."""

    name: Literal["only-platform"]
    region: str | None = None


class OnlyPlatform(ConformingVMPlatform):
    name: ClassVar[str] = "only-platform"
    description: ClassVar[str] = "the sole registered platform"
    config_model: ClassVar[type[AgwModel]] = OnlyConfig


@pytest.fixture
def one_arm() -> Iterator[None]:
    """The live vm-platform registry holding EXACTLY one implementation.

    Seated through the real plugin machinery, then the shipped platforms
    set aside for the duration, so the assertion states what the union IS
    rather than what it contains.
    """
    with seated_plugin(Plugin(name="one-arm", capabilities={"vm-platform": (OnlyPlatform,)})):
        registry = _registry()
        shipped = {name: impl for name, impl in registry.items() if name != OnlyPlatform.name}
        for name in shipped:
            del registry[name]
        try:
            yield
        finally:
            registry.update(shipped)


def _registry() -> dict[str, Any]:
    from agentworks.capabilities.descriptor import descriptor_for

    return descriptor_for("vm-platform").registry()


@pytest.mark.usefixtures("one_arm")
def test_a_one_arm_union_still_carries_its_discriminator() -> None:
    """``Union[(X,)]`` collapses to ``X``, so a capability kind with one
    registered implementation has no union left in its annotation. It is
    not a hypothetical: harness-integration and git-credential-provider
    each ship exactly one.

    Emission classifies on DISCRIMINATOR PRESENCE rather than on whether
    the annotation is still a union, and pydantic keeps the tagged-union
    core schema through the collapse, so the emitted shape is the one a
    second implementation grows into rather than a bare model.
    """
    schema = document_schema("vm-site")
    union = schema["$defs"][schema["$defs"]["VmSiteSpec"]["properties"]["platform"]["$ref"].rsplit("/", 1)[-1]]
    assert union["discriminator"] == {
        "propertyName": "name",
        "mapping": {"only-platform": "#/$defs/OnlyConfig"},
    }
    assert union["oneOf"] == [{"$ref": "#/$defs/OnlyConfig"}]
    Draft202012Validator.check_schema(schema)
    assert _errors(schema, _a_document("vm-site", {"platform": {"name": "only-platform"}})) == []
    assert _errors(schema, _a_document("vm-site", {"platform": {"name": "lima"}}))


def test_the_schema_follows_the_registry_rather_than_a_cache(one_arm: None) -> None:
    """Emission caches nothing across calls. A stale union would validate
    one capability against another's schema, which is a silent wrong
    answer, and the one thing that makes it impossible is not asking a
    cache."""
    assert "OnlyConfig" in document_schema("vm-site")["$defs"]
    with seated_plugin(Plugin(name="one-arm-plus", capabilities={"vm-platform": (SecondPlatform,)})):
        mapping = document_schema("vm-site")["$defs"]["VmPlatformConfig"]["discriminator"]["mapping"]
        assert sorted(mapping) == ["only-platform", "second-platform"]


class SecondConfig(AgwModel):
    """A second arm, seated mid-test."""

    name: Literal["second-platform"]


class SecondPlatform(ConformingVMPlatform):
    name: ClassVar[str] = "second-platform"
    description: ClassVar[str] = "seated mid-test"
    config_model: ClassVar[type[AgwModel]] = SecondConfig


# -- Soundness -------------------------------------------------------------


def test_the_shapes_the_envelope_tolerates_are_not_schema_errors() -> None:
    """Two places the schema could easily be STRICTER than the loader,
    which is the one direction emission may not go.

    ``spec:`` with nothing after it loads as an empty mapping, and
    ``expires`` accepts a date, an RFC 3339 timestamp, and a quoted
    spelling of either (``Expiry``'s before-validator), where the emitted
    type of the validated object would be a bare ``date-time``.
    """
    schema = document_schema("admin-template")
    assert _errors(schema, _a_document("admin-template", None)) == []
    for spelling in ("2026-01-01", "2026-01-01T00:00:00Z", "2026-01-01 00:00:00+00:00"):
        assert _errors(schema, _a_document("admin-template", {}, expires=spelling)) == [], spelling


def test_a_field_the_model_fills_is_not_required() -> None:
    """An unscoped github credential writes nothing but the tag, because
    the marker's owner template supplies the token. Emitting ``token`` as
    required would red-underline the shipped sample.

    ``AgwModel`` owns the correction; this is the end-to-end proof that it
    survives the splice into a hosting kind's document.
    """
    schema = document_schema("git-credential")
    assert "token" not in schema["$defs"]["GitHubConfig"]["required"]
    assert _errors(schema, _a_document("git-credential", {"provider": {"name": "github"}})) == []


def test_emitted_schemas_accept_every_bundled_sample_document() -> None:
    """The automated half of the editor-association check: the documents
    the real loader accepts also validate against what an editor would be
    handed, against BOTH the per-kind schema and the any-kind one.

    The shipped plugins' platforms have to be seated, because the vm-site
    sample declares azure, aws, and proxmox sites and the emitted union
    describes what this host has REGISTERED. Importing
    ``agentworks.plugins`` is what seats them (its module body registers
    every shipped plugin), which this module's own imports already did;
    the assert makes that dependency visible rather than lucky.
    """
    assert {"azure-vm", "aws-ec2", "proxmox"} <= _platform_names()
    envelope = envelope_schema()
    for kind in SAMPLE_KINDS:
        text = _uncomment(sample_text(kind))
        documents = _documents(text)
        per_kind = document_schema(kind)
        for document in documents:
            assert _errors(per_kind, document) == [], (kind, document.get("metadata"))
            assert _errors(envelope, document) == [], (kind, document.get("metadata"))


def test_the_sample_set_still_loads_through_the_real_loader(tmp_path: Path) -> None:
    """The other half of the pairing above: what the schemas were just
    asked to accept is what the loader accepts, so "sound
    under-approximation" is a claim about the same documents."""
    resources = tmp_path / "resources"
    resources.mkdir()
    for kind in SAMPLE_KINDS:
        (resources / f"{kind}.yaml").write_text(_uncomment(sample_text(kind)))
    manifests = load_manifests(resources)
    assert not manifests.issues, manifests.issues


def test_reference_markers_reach_emitted_schema() -> None:
    """Emission and the field-reference stream are SIBLING derivations
    from the models, and the marker's own schema hook is the seam. A
    marked field's ``x-agw-ref`` arriving in an emitted document is what
    says the two still read the same authored fact."""
    from agentworks.schema import REF_SCHEMA_KEY

    schema = document_schema("git-credential")
    marked = [
        prop
        for definition in schema["$defs"].values()
        for prop in definition.get("properties", {}).values()
        if REF_SCHEMA_KEY in prop
    ]
    assert marked, "no x-agw-ref survived into the git-credential schema"
    assert all(set(prop[REF_SCHEMA_KEY]) == {"kind", "usage", "default_template", "relationship"} for prop in marked)


# -- Writing, and the modeline ---------------------------------------------


def test_write_schema_set_writes_readable_json_the_loader_ignores(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    written = write_schema_set(resources / SCHEMA_DIRNAME)
    assert {path.name for path in written} == set(schema_set())
    for path in written:
        Draft202012Validator.check_schema(json.loads(path.read_text()))
    # Dot-prefixed on purpose: the manifest walk prunes dot-directories,
    # so a generated artifact can never be read as a declaration.
    assert not load_manifests(resources).entries


def test_write_schema_set_overwrites_a_stale_schema(tmp_path: Path) -> None:
    """These are derived artifacts whose only correct content is what the
    current registry implies, so a leftover file would associate an
    operator's manifests with a schema that no longer describes them."""
    schema_dir = tmp_path / SCHEMA_DIRNAME
    write_schema_set(schema_dir)
    stale = schema_dir / ENVELOPE_SCHEMA_FILENAME
    stale.write_text("{}")
    write_schema_set(schema_dir)
    assert json.loads(stale.read_text())["discriminator"]["propertyName"] == "kind"


def test_the_modeline_is_relative_to_the_manifest(tmp_path: Path) -> None:
    """Relative so the resources directory stays portable: moving or
    copying it does not break the association."""
    resources = tmp_path / "resources"
    flat = modeline(manifest_path=resources / "all.yaml", resources_dir=resources, kind=None)
    assert flat == f"# yaml-language-server: $schema={SCHEMA_DIRNAME}/{ENVELOPE_SCHEMA_FILENAME}"

    nested = modeline(
        manifest_path=resources / "vm-template" / "small.yaml",
        resources_dir=resources,
        kind="vm-template",
    )
    assert nested == f"# yaml-language-server: $schema=../{SCHEMA_DIRNAME}/vm-template.schema.json"
