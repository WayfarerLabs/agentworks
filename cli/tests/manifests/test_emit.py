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
import re
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import pytest
import yaml
from jsonschema import Draft202012Validator

from agentworks.declared_resource import METADATA_FIELDS
from agentworks.manifests.emit import (
    ENVELOPE_SCHEMA_FILENAME,
    SCHEMA_DIRNAME,
    YAML_11_ONLY_BOOLEANS,
    document_schema,
    envelope_schema,
    modeline,
    schema_filename,
    schema_set,
    write_schema_set,
)
from agentworks.manifests.envelope import _ENVELOPE_KEYS, API_VERSION
from agentworks.manifests.loader import load_manifests
from agentworks.manifests.samples import sample_text
from agentworks.manifests.spec_model import declarable_kinds
from agentworks.plugins import Plugin, seated_plugin
from agentworks.resources import KIND_REGISTRY
from agentworks.schema import AgwModel
from tests._emitted_schema import ref_extension
from tests.plugins._fixtures import ConformingVMPlatform

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class _EditorLoader(yaml.SafeLoader):
    """A YAML load that sees what a SCHEMA-AWARE EDITOR sees.

    yaml-language-server reads the document's syntax tree under YAML 1.2's
    core schema; the loader is pyyaml's safe loader, which resolves YAML
    1.1. The two disagree on exactly two things a manifest can contain,
    and this class is where the difference is modelled, because a test
    validating pyyaml's answer would be testing a document no editor ever
    holds:

    - **timestamps.** 1.2 core has no implicit timestamp type, so
      ``2026-01-01`` reaches the editor's validator as a string where
      pyyaml makes it a ``datetime.date``.
    - **booleans.** 1.1 resolves ``yes`` / ``no`` / ``on`` / ``off`` (in
      three casings each) to booleans; 1.2 core recognizes only
      ``true`` / ``false``, so those twelve reach the validator as
      strings. Modelling the timestamps and NOT the booleans is what let
      an over-strict ``"type": "boolean"`` ship: every rendered sample
      spells its booleans ``true`` / ``false``, so no document this
      harness held ever exercised the difference.
    """


_YAML_12_BOOLEANS = frozenset({"true", "True", "TRUE", "false", "False", "FALSE"})
"""The only plain scalars YAML 1.2 core resolves to a boolean."""

_EditorLoader.yaml_implicit_resolvers = {
    first: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag not in {"tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:bool"}
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _first in "tTfF":
    _EditorLoader.yaml_implicit_resolvers[_first].insert(
        0,
        ("tag:yaml.org,2002:bool", re.compile(f"^(?:{'|'.join(_YAML_12_BOOLEANS)})$")),
    )


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
    than listed. Emission and the sample surface now call the SAME
    derivation (``spec_model.declarable_kinds``), so the two cannot come to
    describe different kinds; what is left to pin is that the derivation
    itself is the registry's category and that the written set matches."""
    declarable = {name for name, handler in KIND_REGISTRY.items() if handler.category == "declarable"}
    assert set(declarable_kinds()) == declarable
    assert sorted(schema_set()) == sorted(
        [ENVELOPE_SCHEMA_FILENAME, *(schema_filename(kind) for kind in declarable_kinds())]
    )


def test_every_emitted_schema_meta_validates_as_2020_12() -> None:
    """The reference implementation checking our output against the
    2020-12 metaschema: what a hand-written assertion would not notice is
    a keyword misused or misspelled anywhere in the tree."""
    for filename, schema in schema_set().items():
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema", filename
        Draft202012Validator.check_schema(schema)


def test_every_ref_is_local_and_resolves() -> None:
    """The check ``check_schema`` does NOT make, verified: a dangling
    ``$ref`` passes meta-validation (``check_schema({"$ref":
    "#/$defs/nope"})`` raises nothing), and ``iter_errors`` only catches
    one on a branch some document happens to exercise.

    A ``$defs`` graph nobody writes by hand is exactly where a dangling
    pointer would go unseen, so it gets its own walk. Locality matters
    too: an emitted file that reached outside itself would make an
    editor's view depend on network or filesystem resolution.
    """
    for filename, schema in schema_set().items():
        defs = schema.get("$defs", {})
        for ref in _every_ref(schema):
            assert ref.startswith("#/$defs/"), f"{filename}: non-local $ref {ref}"
            assert ref.removeprefix("#/$defs/") in defs, f"{filename}: dangling $ref {ref}"


def _every_ref(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref
        for value in node.values():
            yield from _every_ref(value)
    elif isinstance(node, list):
        for item in node:
            yield from _every_ref(item)


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


def test_every_metadata_field_of_every_kind_carries_hover_text() -> None:
    """``metadata`` is the one block every document writes, whatever its
    kind, so a missing description there is a blank hover on the most-read
    surface the schema has. Per KIND, because a kind that re-declares a
    metadata field supplies its own docstring or none.
    """
    blank = [
        (kind, field)
        for kind in declarable_kinds()
        for field, prop in _metadata_properties(kind).items()
        if not prop.get("description")
    ]
    assert not blank


def _metadata_properties(kind: str) -> dict[str, Any]:
    schema = document_schema(kind)
    metadata: dict[str, Any] = schema["$defs"][schema["properties"]["metadata"]["$ref"].rsplit("/", 1)[-1]]
    return metadata["properties"]  # type: ignore[no-any-return]


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
    assert set(schema["discriminator"]["mapping"]) == set(declarable_kinds())
    assert len(schema["oneOf"]) == len(declarable_kinds())


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


def test_the_union_carries_the_platforms_shipped_plugins_contribute() -> None:
    """A plugin's capability reaches the emitted schema, by name.

    Named plugins rather than a count, and rather than the live registry
    (which is what ``_platform_names`` reads, so it would have agreed with
    a truncated union). What this does NOT prove is that the seating step
    is doing it: three test modules import ``agentworks.plugins`` at module
    scope for their own fixtures, so by the time this runs, the process is
    seated whatever ``spec_model`` does. That guard has to run in a fresh
    interpreter and lives in ``tests/manifests/test_spec_model.py``; this
    one is about the emitted SHAPE carrying the names."""
    mapping = _vm_platform_union()["discriminator"]["mapping"]
    assert {"azure-vm", "aws-ec2", "proxmox"} <= set(mapping)


def _vm_platform_union() -> dict[str, Any]:
    schema = document_schema("vm-site")
    platform = schema["$defs"]["VmSiteSpec"]["properties"]["platform"]
    union = schema["$defs"][platform["$ref"].rsplit("/", 1)[-1]]
    assert isinstance(union, dict)
    return union


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


def test_a_field_the_model_fills_is_neither_required_nor_non_nullable() -> None:
    """An unscoped github credential writes nothing but the tag, because
    the marker's owner template supplies the token, and writing
    ``token: null`` says the same thing out loud. Both load, so both have
    to validate.

    ``AgwModel`` owns the correction; this is the end-to-end proof that it
    survives the splice into a hosting kind's document.
    """
    schema = document_schema("git-credential")
    assert "token" not in schema["$defs"]["GitHubConfig"]["required"]
    assert _errors(schema, _a_document("git-credential", {"provider": {"name": "github"}})) == []
    assert _errors(schema, _a_document("git-credential", {"provider": {"name": "github", "token": None}})) == []


def test_the_splice_keeps_an_optional_blocks_null_arm() -> None:
    """``session-template``'s ``harness_integration`` is
    ``CapabilityBlock | None = None``, and the splice replaces the field's
    MODEL, not the rest of what the row declares about it.

    Dropping the null arm would reject ``harness_integration: null``,
    which loads, and would leave the property carrying ``default: null``
    against a subschema that refuses null, so an editor's insert-default
    would produce config the same schema flags.
    """
    schema = document_schema("session-template")
    block = schema["$defs"]["SessionTemplateSpec"]["properties"]["harness_integration"]
    assert block["default"] is None
    assert {"type": "null"} in block["anyOf"]
    assert _errors(schema, _a_document("session-template", {"harness_integration": None})) == []

    # The non-optional sibling keeps its bare $ref: the null arm follows
    # the row's declaration, it is not added to every spliced field.
    platform = document_schema("vm-site")["$defs"]["VmSiteSpec"]["properties"]["platform"]
    assert "anyOf" not in platform
    assert platform["$ref"].startswith("#/$defs/")


def test_emitted_schemas_accept_every_document_the_full_load_path_accepts(tmp_path: Path) -> None:
    """The soundness contract, over the whole bundled sample set, checked
    against the FULL load path rather than against decode.

    ``load_manifests`` alone would not settle it: capability config is
    checked at finalize, not at decode, so a document with an unknown key
    inside a capability block passes the loader and is refused by
    ``build_registry``. Running the registry build is what makes "the
    loader accepts these" mean what the contract needs it to mean, and it
    is why the two halves are ONE test over ONE set of documents rather
    than two that could drift onto different inputs.

    The shipped plugins' platforms have to be seated, because the emitted
    union describes what this host has REGISTERED and the rendered samples
    are validated against it. Seating is the emitter's own responsibility
    now (``spec_model`` calls ``seat_installed_plugins``), so the assert
    below is a premise check rather than a dependency this file arranges:
    if it ever fails, the schemas these documents are checked against
    describe a smaller host than the one that rendered them.
    """
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config

    assert {"azure-vm", "aws-ec2", "proxmox"} <= _platform_names()

    resources = tmp_path / "resources"
    resources.mkdir()
    for kind in declarable_kinds():
        (resources / f"{kind}.yaml").write_text(_uncomment(sample_text(kind)))
    build_registry(load_config(_a_config(tmp_path), warn_issues=False))

    envelope = envelope_schema()
    for kind in declarable_kinds():
        per_kind = document_schema(kind)
        for document in _documents((resources / f"{kind}.yaml").read_text()):
            assert _errors(per_kind, document) == [], (kind, document.get("metadata"))
            assert _errors(envelope, document) == [], (kind, document.get("metadata"))


def _a_config(root: Path) -> Path:
    """A config whose plugins are enabled, so the sample's azure, aws, and
    proxmox sites reach their platform's own model at finalize rather than
    sitting disabled and unchecked."""
    (root / "id.pub").write_text("ssh-ed25519 AAAA...")
    (root / "id").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    cfg = root / "config.toml"
    cfg.write_text(
        f"""\
[operator]
ssh_public_key = "{(root / "id.pub").as_posix()}"
ssh_private_key = "{(root / "id").as_posix()}"

[plugins]
system = ["azure", "aws", "proxmox"]
"""
    )
    return cfg


# -- Booleans: the loader's YAML version is not the editor's ---------------


def _loader_boolean_spellings() -> set[str]:
    """Every plain scalar the LOADER resolves to a boolean.

    Derived from pyyaml's own table and confirmed through a real load,
    rather than transcribed from the YAML 1.1 spec: if a pyyaml release
    ever adds or drops a spelling, this moves with it and the assertions
    below fail instead of quietly describing an older parser.
    """
    words = yaml.constructor.SafeConstructor.bool_values
    casings = {casing for word in words for casing in (word.lower(), word.upper(), word.capitalize())}
    return {text for text in casings if isinstance(yaml.safe_load(f"a: {text}")["a"], bool)}


def test_the_widened_spellings_are_exactly_where_the_two_parsers_disagree() -> None:
    """``YAML_11_ONLY_BOOLEANS`` is neither short nor long.

    Short would put the over-reporting back for the spellings it missed;
    long would have the schema accept a string the loader refuses, for no
    reason. Both halves are derived, so neither list is maintained by
    hand.
    """
    loader_spellings = _loader_boolean_spellings()

    # The editor's set is a subset of the loader's: the two agree that
    # `true` / `false` are booleans, and differ only by what 1.1 adds.
    assert loader_spellings >= _YAML_12_BOOLEANS
    assert loader_spellings - _YAML_12_BOOLEANS == set(YAML_11_ONLY_BOOLEANS)


def test_no_emitted_boolean_type_is_narrower_than_the_loader() -> None:
    """No bare ``"type": "boolean"`` survives anywhere in the set.

    The widening lives in the schema GENERATOR, so it covers every
    boolean automatically; this is the guard for a future emission path
    that assembles a schema without going through it.

    ``const`` nodes are exempt, and there is exactly one: the
    ``Literal[False]`` opt-out arm of a secret's ``backend_mappings``.
    It cannot over-report on its own, because it only ever appears in a
    union beside the string arm that carries an identifier override, and
    a string is what a 1.2 editor has in hand. The test below proves
    that rather than leaving it as an argument.
    """
    for filename, schema in schema_set().items():
        for node in _every_subschema(schema):
            if "const" in node:
                continue
            assert node.get("type") != "boolean", (filename, node)


def test_a_yaml_11_opt_out_is_not_a_schema_error() -> None:
    """``backend_mappings: {env-var: no}`` is an opt-out to the loader and
    a string to the editor, and the schema flags neither.

    This is the exemption above, executed. The editor reads the string
    as an identifier override rather than as the opt-out it is, which is
    a difference in what the two UNDERSTAND rather than a diagnostic on
    valid config; no schema can settle it, because ``"no"`` is a
    perfectly good identifier and the arms are genuinely ambiguous.
    """
    text = (
        f"apiVersion: {API_VERSION}\n"
        "kind: secret\n"
        "metadata:\n"
        "  name: npm-token\n"
        "  description: npm registry token\n"
        "spec:\n"
        "  backend_mappings:\n"
        "    env-var: no\n"
    )
    (document,) = _documents(text)

    assert document["spec"]["backend_mappings"]["env-var"] == "no"
    assert _errors(document_schema("secret"), document) == []


def _every_subschema(node: object) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _every_subschema(value)
    elif isinstance(node, list):
        for value in node:
            yield from _every_subschema(value)


def _an_apt_source(dearmor: str) -> str:
    return (
        f"apiVersion: {API_VERSION}\n"
        "kind: apt-source\n"
        "metadata:\n"
        "  name: docker\n"
        "spec:\n"
        "  key_url: https://example.com/k.gpg\n"
        "  key_path: /etc/apt/keyrings/docker.gpg\n"
        f"  key_dearmor: {dearmor}\n"
        "  source: deb https://example.com stable main\n"
        "  source_file: docker.list\n"
    )


@pytest.mark.parametrize("spelling", YAML_11_ONLY_BOOLEANS)
def test_a_yaml_11_boolean_spelling_is_not_a_schema_error(spelling: str, tmp_path: Path) -> None:
    """The soundness contract at the one place the two parsers disagree.

    ``key_dearmor: no`` is a perfectly ordinary way to write this field
    and the loader reads it as ``False``. The editor hands its validator
    the STRING ``"no"``, so a boolean-only schema red-underlined valid
    configuration, which is the one direction emission may not go.

    Both halves are asserted over the SAME text: what the editor sees is
    accepted, and what the loader does with it is what the operator
    meant.
    """
    text = _an_apt_source(spelling)

    (document,) = _documents(text)
    assert isinstance(document["spec"]["key_dearmor"], str), "the editor sees a string, or this proves nothing"
    assert _errors(document_schema("apt-source"), document) == []
    assert _errors(envelope_schema(), document) == []

    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "docker.yaml").write_text(text)
    (entry,) = load_manifests(resources).entries
    assert entry.resource.key_dearmor is (spelling.lower() in {"yes", "on"})


def test_a_string_that_is_not_a_boolean_spelling_is_still_a_schema_error() -> None:
    """The widening is a widening, not a hole: everything else a string
    could say is still flagged, and so is a value of the wrong type."""
    schema = document_schema("apt-source")
    (nonsense,) = _documents(_an_apt_source("maybe"))
    (number,) = _documents(_an_apt_source("7"))

    assert _errors(schema, nonsense)
    assert _errors(schema, number)


def test_reference_markers_reach_emitted_schema() -> None:
    """Emission and the field-reference stream are SIBLING derivations
    from the models, and the marker's own schema hook is the seam. A
    marked field's ``x-agw-ref`` arriving in an emitted document is what
    says the two still read the same authored fact.

    Searched recursively rather than at the property's top level: a
    templated field is widened with a null arm, and the marker rides the
    constrained arm, exactly as it does for a natively optional one.
    """
    schema = document_schema("git-credential")
    marked = [
        extension
        for definition in schema["$defs"].values()
        for prop in definition.get("properties", {}).values()
        if (extension := ref_extension(prop)) is not None
    ]
    assert marked, "no x-agw-ref survived into the git-credential schema"
    assert all(set(extension) == {"kind", "usage", "default_template", "relationship"} for extension in marked)


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
