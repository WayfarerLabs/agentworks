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
    YAML_11_ONLY_INTEGERS,
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
from agentworks.schema import REF_SCHEMA_KEY, AgwModel
from tests._emitted_schema import ref_extension
from tests.manifests.conftest import uncomment
from tests.plugins._fixtures import ConformingVMPlatform

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class _EditorLoader(yaml.SafeLoader):
    """A YAML load that sees what a SCHEMA-AWARE EDITOR sees.

    yaml-language-server reads the document's syntax tree under YAML 1.2's
    core schema; the loader is pyyaml's safe loader, which resolves YAML
    1.1. The two disagree on three things a manifest can contain, and this
    class is where the difference is modelled, because a test validating
    pyyaml's answer would be testing a document no editor ever holds:

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
    - **integers.** 1.1 adds underscore separators (``8_192``),
      sexagesimal (``1:30``), binary (``0b1010``) and signed hex
      (``+0x1F``) on top of what 1.2 core resolves, so those reach the
      validator as strings. The same blind spot as the booleans, found
      the same way and one round later: modelling two of the three left
      ``"type": "integer"`` over-reporting on ``memory: 8_192``.

    Floats are NOT modelled, and the one visible consequence is that
    ``1e3`` is a string here where a real editor makes it the number
    1000. It is inert because the emitted surface holds no float field,
    and ``test_the_float_gap_is_still_unreachable`` is what keeps that
    true; adding one means finishing this harness as well as
    ``emit._ManifestJsonSchema``.
    """


_YAML_12_BOOLEANS = frozenset({"true", "True", "TRUE", "false", "False", "FALSE"})
"""The only plain scalars YAML 1.2 core resolves to a boolean."""

_YAML_12_INTEGER = re.compile(r"^(?:[-+]?[0-9]+|0x[0-9a-fA-F]+|0o[0-7]+)$")
"""The plain scalars YAML 1.2 core resolves to an integer.

Note what is missing against 1.1 and what is added: no underscores and no
sexagesimal, hex takes no sign, and ``0o17`` is an integer to 1.2 alone.
"""

_EditorLoader.yaml_implicit_resolvers = {
    first: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag not in {"tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:bool", "tag:yaml.org,2002:int"}
    ]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _first in "tTfF":
    _EditorLoader.yaml_implicit_resolvers[_first].insert(
        0,
        ("tag:yaml.org,2002:bool", re.compile(f"^(?:{'|'.join(_YAML_12_BOOLEANS)})$")),
    )
for _first in "-+0123456789":
    _EditorLoader.yaml_implicit_resolvers[_first].insert(0, ("tag:yaml.org,2002:int", _YAML_12_INTEGER))


def _construct_yaml_12_integer(_loader: yaml.SafeLoader, node: yaml.Node) -> int:
    """1.2 core's VALUE for an integer, not merely its type.

    The only member of this file's 1.2 modelling that has to touch a
    CONSTRUCTOR rather than a resolver, and the reason is the one
    disagreement no schema can catch: ``010`` is a leading-zero octal to
    1.1 and a plain decimal to 1.2, so both parsers hand the validator an
    integer and they differ only in that it is 8 or 10. Leaving pyyaml's
    constructor in place would make this harness report 8 and hide it.

    pyyaml's ``add_constructor`` copies the table onto the subclass before
    writing, so ``SafeLoader`` is untouched by this.
    """
    text = str(node.value)
    if text[:2].lower() == "0x":
        return int(text, 16)
    if text[:2].lower() == "0o":
        return int(text, 8)
    return int(text, 10)


_EditorLoader.add_constructor("tag:yaml.org,2002:int", _construct_yaml_12_integer)


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
    describe different kinds; what is left to pin here is that the WRITTEN
    set matches it.

    That the derivation is itself the registry's category is pinned where
    the derivation lives, in
    ``test_spec_model.py::test_declarable_kinds_is_the_registry_category_and_nothing_else``.
    """
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


# The two refusals ``document_schema`` makes (an unknown kind, and a
# capability kind, which is declared in code and carries no manifest) are
# pinned one layer up, in
# ``test_schema_command.py::test_a_bad_kind_is_a_clean_domain_error``: same
# two inputs, and it reaches these raise sites through the real CLI, so it
# also pins that they arrive as one clean ``Error:`` line with a hint.


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


def test_env_sources_emit_as_an_untagged_structural_one_of() -> None:
    schema = document_schema("vm-template")
    env_entry = schema["$defs"]["EnvEntry"]

    assert "anyOf" not in env_entry
    assert env_entry["oneOf"] == [
        {"$ref": "#/$defs/PlaintextEnvEntry"},
        {"$ref": "#/$defs/SecretEnvEntry"},
    ]
    assert schema["$defs"]["PlaintextEnvEntry"]["anyOf"][1]["required"] == ["value"]
    assert schema["$defs"]["SecretEnvEntry"]["required"] == ["secret"]


@pytest.mark.parametrize(
    "entry",
    ["text", {"value": "text"}, {"secret": "api-token"}],
)
def test_the_emitted_schema_accepts_every_env_source_spelling(entry: object) -> None:
    document = {
        "apiVersion": API_VERSION,
        "kind": "vm-template",
        "metadata": {"name": "env-shape"},
        "spec": {"env": {"TOKEN": entry}},
    }
    assert _errors(document_schema("vm-template"), document) == []


@pytest.mark.parametrize("entry", [{}, {"value": "text", "secret": "api-token"}])
def test_the_emitted_schema_rejects_env_tables_that_match_no_arm(entry: object) -> None:
    document = {
        "apiVersion": API_VERSION,
        "kind": "vm-template",
        "metadata": {"name": "env-shape"},
        "spec": {"env": {"TOKEN": entry}},
    }
    assert _errors(document_schema("vm-template"), document)


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
    description.

    The mapping is compared against the live registry, so this is also
    where a TRUNCATED union fails. A separate test naming
    ``azure-vm``/``aws-ec2``/``proxmox`` stood here and is gone: the only
    mutation it caught that this one does not is a registry missing the
    plugins altogether, which is the seating direction, and that has to be
    checked in a fresh interpreter
    (``test_spec_model.py::test_the_plugin_platforms_are_present_in_a_FRESH_interpreter``).
    """
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


# What the splice buys (without it the block is ``extra="allow"`` and an
# editor would accept anything) is pinned by the two halves of the
# soundness contract further down rather than by a test of its own:
# ``test_a_capability_key_the_schema_rejects_is_rejected_on_every_host``
# opens on the same misspelled ``vm_host`` inside the same ``lima`` block
# and goes on to prove the LOADER agrees, and
# ``test_emitted_schemas_accept_every_document_the_full_load_path_accepts``
# validates the shipped vm-site sample, capability block and all.


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
    not a hypothetical: with no plugins seated, harness-integration and
    git-credential-provider each have exactly one BUILT-IN, and that is a
    real configuration a host can be in. Every shipped plugin set grows
    them past one (harness-integration to three, git-credential-provider
    to two), so a registry read with plugins seated will not show you this
    case. An earlier note in this effort claimed the collapse was live in
    the shipped registry; it is live in the core-only one.

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


def test_git_token_acquisition_is_a_defaulted_one_arm_discriminated_union() -> None:
    """An unscoped github credential writes nothing but the provider tag.

    Token acquisition remains a real tagged union with one stored arm;
    omission defaults to that historical behavior, and a bare secret name
    remains the stored arm's shorthand. Explicit ``token: null`` is the
    one retired spelling and therefore is not offered by emitted schema.

    ``AgwModel`` owns the correction; this is the end-to-end proof that it
    survives the splice into a hosting kind's document.
    """
    schema = document_schema("git-credential")
    token = schema["$defs"]["GitHubConfig"]["properties"]["token"]
    assert token["default"] == {"mode": "stored"}
    assert token["anyOf"][0] == {"type": "string"}
    tagged = token["anyOf"][1]
    assert tagged["discriminator"] == {
        "propertyName": "mode",
        "mapping": {"stored": "#/$defs/StoredToken"},
    }
    assert tagged["oneOf"] == [{"$ref": "#/$defs/StoredToken"}]
    assert _errors(schema, _a_document("git-credential", {"provider": {"name": "github"}})) == []
    assert _errors(schema, _a_document("git-credential", {"provider": {"name": "github", "token": "custom"}})) == []
    assert (
        _errors(
            schema,
            _a_document(
                "git-credential",
                {"provider": {"name": "github", "token": {"mode": "stored", "secret": "custom"}}},
            ),
        )
        == []
    )
    assert _errors(schema, _a_document("git-credential", {"provider": {"name": "github", "token": None}}))


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
        (resources / f"{kind}.yaml").write_text(uncomment(sample_text(kind)))
    build_registry(load_config(_a_config(tmp_path), warn_issues=False))

    envelope = envelope_schema()
    for kind in declarable_kinds():
        per_kind = document_schema(kind)
        for document in _documents((resources / f"{kind}.yaml").read_text()):
            assert _errors(per_kind, document) == [], (kind, document.get("metadata"))
            assert _errors(envelope, document) == [], (kind, document.get("metadata"))


@pytest.mark.parametrize("limactl", [None, "/usr/bin/limactl"])
def test_a_capability_key_the_schema_rejects_is_rejected_on_every_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limactl: str | None
) -> None:
    """The soundness contract in the REJECTS direction, which is the one the
    statement in ``emit.py`` actually makes and the one that was false.

    A misspelled ``vm_host`` is underlined by the emitted schema on any host,
    because ``LimaConfig`` is closed and the splice carries that into the
    document schema. The loader has to agree on any host too, or an operator
    fixes a squiggle their machine never would have complained about, and a
    teammate on a different machine gets the opposite advice from the same two
    tools.

    ``limactl`` is what made the two disagree. The finalize validate pass was
    readiness-gated, and ``lima.not_ready`` read the host key off unvalidated
    config, so the typo made the site look local, a local site without
    ``limactl`` is not-ready, and a not-ready site was not validated. The
    schema said no and the loader said yes, but only on hosts without Lima
    installed. Both spellings of the host are pinned here so the parametrize
    fails loudly if validation is ever gated on the environment again.

    The typo now sits INSIDE the placement arm, which is where a host key
    lives; the required ``placement`` union has since removed the other
    half of the original defect, since ``not_ready`` keys on the tag
    rather than on the key's presence and so cannot be fooled by a
    misspelling at all."""
    from agentworks.bootstrap import build_registry
    from agentworks.config import load_config
    from agentworks.errors import ConfigError

    monkeypatch.setattr("shutil.which", lambda name, found=limactl: found if name == "limactl" else None)
    typo = _a_document("vm-site", {"platform": {"name": "lima", "placement": {"mode": "ssh", "hst": "me@box"}}})
    assert _errors(document_schema("vm-site"), typo), "premise: the schema must reject this document"

    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "sites.yaml").write_text(yaml.safe_dump(typo))
    with pytest.raises(ConfigError, match="placement.hst: unknown field"):
        build_registry(load_config(_a_config(tmp_path), warn_issues=False))


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


# -- Integers: the same disagreement, one type over -------------------------


def _loader_integer_pattern() -> str:
    """pyyaml's own ``int`` implicit resolver, as an ECMA-262 pattern.

    Read off the live resolver rather than transcribed, for the reason
    ``_loader_boolean_spellings`` is derived: a pyyaml release that moves
    the language should fail the assertion below rather than leave the
    emitted schema quietly describing an older parser.

    The pattern is compiled ``re.VERBOSE``, where unescaped whitespace is
    layout, so stripping it is the whole conversion. That stripping is
    what ``test_the_widened_integer_pattern_is_pyyamls_own_language``
    checks by BEHAVIOR rather than by eye, since it would stop being
    lossless the day pyyaml puts a space inside a character class.
    """
    for resolvers in yaml.SafeLoader.yaml_implicit_resolvers.values():
        for tag, regexp in resolvers:
            if tag == "tag:yaml.org,2002:int":
                return re.sub(r"\s+", "", regexp.pattern)
    raise AssertionError("pyyaml has no implicit int resolver")


#: Every spelling below is measured against BOTH real parsers: pyyaml here,
#: and the `yaml` npm package (yaml-language-server's own, at its 1.2 core
#: defaults) at the versions recorded in the emission LLD's section 2.3.
_OVER_REPORTING_INTEGERS = (
    ("8_192", 8192),
    ("1_000_000", 1000000),
    ("1:30", 90),
    ("1:30:00", 5400),
    ("0b1010", 10),
    ("0b1_010", 10),
    ("0x1_F", 31),
    ("+0x1F", 31),
    ("-0x1F", -31),
    ("0_7", 7),
)
"""``(source text, what the loader reads)`` where the editor sees a string.

The direction section 2 forbids: a bare ``"type": "integer"`` flags every
one of these, and all of them load.
"""


def test_the_widened_integer_pattern_is_pyyamls_own_language_minus_yaml_12() -> None:
    """``YAML_11_ONLY_INTEGERS`` is derived, not hand-listed.

    Rebuilt here from the two halves it is made of, the same shape as the
    boolean assertion above: pyyaml's live resolver for what the loader
    reads as an integer, and ``_YAML_12_INTEGER`` for what an editor
    resolves without help. Neither half is maintained by hand.

    The whitespace strip is checked by behavior over the corpus below
    rather than by comparing pattern text, so a pyyaml pattern that
    stopped surviving it would fail here instead of silently narrowing
    every emitted integer.
    """
    loader_pattern = _loader_integer_pattern()
    assert loader_pattern.startswith("^"), loader_pattern
    rebuilt = f"^(?!{_YAML_12_INTEGER.pattern[1:-1]}$){loader_pattern[1:]}"

    assert rebuilt == YAML_11_ONLY_INTEGERS

    # The strip was lossless: pyyaml's compiled resolver and the stripped
    # pattern agree on every spelling either might see.
    stripped = re.compile(loader_pattern)
    for text, _ in (*_OVER_REPORTING_INTEGERS, ("010", 8), ("5", 5), ("0o17", 0), ("_1", 0), ("1:60", 0)):
        loader_reads_int = isinstance(yaml.safe_load(f"a: {text}")["a"], int)
        assert bool(stripped.match(text)) is loader_reads_int, text


def test_no_emitted_integer_type_is_narrower_than_the_loader() -> None:
    """No bare ``"type": "integer"`` survives anywhere in the set.

    The boolean guard's twin, and it is the assertion that was missing:
    nothing pinned the integer shape, so the widening skipped a type
    without any test noticing.
    """
    for filename, schema in schema_set().items():
        for node in _every_subschema(schema):
            assert node.get("type") != "integer", (filename, node)


def test_the_float_gap_is_still_unreachable() -> None:
    """No emitted field is a float, which is what lets there be no
    ``float_schema`` override.

    pyyaml reads ``1_000.5`` as a float where 1.2 core reads a string, so
    a float field would arrive over-reporting exactly as integers did.
    Rather than write a widening for a field that does not exist and
    guess at its constraints, this fails the day one appears. Whoever
    that is owes ``_ManifestJsonSchema`` a ``float_schema``, this file's
    ``_EditorLoader`` the matching resolver, and the emission LLD a row.
    """
    for filename, schema in schema_set().items():
        for node in _every_subschema(schema):
            declared = node.get("type")
            declared = declared if isinstance(declared, list) else [declared]
            assert "number" not in declared, (filename, node)


def _a_vm_template(memory: str) -> str:
    return f"apiVersion: {API_VERSION}\nkind: vm-template\nmetadata:\n  name: dev\nspec:\n  memory: {memory}\n"


@pytest.mark.parametrize(("spelling", "loaded"), _OVER_REPORTING_INTEGERS)
def test_a_yaml_11_integer_spelling_is_not_a_schema_error(spelling: str, loaded: int, tmp_path: Path) -> None:
    """The soundness contract at the second place the two parsers
    disagree.

    ``memory: 8_192`` is how an operator writes eight thousand of
    something, and the loader reads ``8192``. The editor hands its
    validator the STRING ``"8_192"``, so a bare ``"type": "integer"``
    red-underlined valid configuration.

    Both halves are asserted over the SAME text, as the boolean pairing
    does: what the editor sees is accepted, and what the loader does with
    it is what the operator meant.
    """
    text = _a_vm_template(spelling)

    (document,) = _documents(text)
    assert isinstance(document["spec"]["memory"], str), "the editor sees a string, or this proves nothing"
    assert _errors(document_schema("vm-template"), document) == []
    assert _errors(envelope_schema(), document) == []

    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "dev.yaml").write_text(text)
    (entry,) = load_manifests(resources).entries
    assert entry.resource.memory == loaded


def test_a_string_that_is_not_an_integer_spelling_is_still_a_schema_error() -> None:
    """The widening is a widening, not a hole.

    A quoted integer is the case worth having: ``"5"`` is a string the
    strict loader refuses, and the subtraction in
    ``YAML_11_ONLY_INTEGERS`` is the only reason the schema can still say
    so. Nothing distinguishes a quoted ``"8_192"`` from a bare one, which
    is why that residual is recorded rather than tested away.
    """
    schema = document_schema("vm-template")
    (nonsense,) = _documents(_a_vm_template("lots"))
    (quoted,) = _documents(_a_vm_template('"5"'))

    assert _errors(schema, nonsense)
    assert _errors(schema, quoted)


def test_a_leading_zero_integer_is_a_value_disagreement_no_schema_can_reach() -> None:
    """``010`` is 8 to the loader and 10 to the editor, and both are
    integers.

    The one member of this class that the widening does NOT address, and
    cannot: the schema sees a conforming integer either way, so there is
    no keyword that could flag it. Pinned so it stays a known shape
    rather than resurfacing as a surprise, and recorded in the emission
    LLD beside the two the schema does answer.
    """
    (document,) = _documents(_a_vm_template("010"))

    assert document["spec"]["memory"] == 10, "1.2 core reads a plain decimal"
    assert yaml.safe_load(_a_vm_template("010"))["spec"]["memory"] == 8, "1.1 reads octal"
    assert _errors(document_schema("vm-template"), document) == []


def test_reference_markers_reach_emitted_schema() -> None:
    """Emission and the field-reference stream are SIBLING derivations
    from the models, and the marker's own schema hook is the seam. A
    marked field's ``x-agw-ref`` arriving in an emitted document is what
    says the two still read the same authored fact.

    Read through ``ref_extension``, which searches the subtree: a field's
    own marker is on the property, and a collection's element marker is on
    ``items``, so one reader covers both without knowing which shape it
    was handed.
    """
    schema = document_schema("git-credential")
    marked = [
        extension for definition in schema["$defs"].values() if (extension := ref_extension(definition)) is not None
    ]
    assert marked, "no x-agw-ref survived into the git-credential schema"
    assert all(set(extension) == {"kind", "usage", "default_template", "relationship"} for extension in marked)


def test_the_stored_token_arm_states_its_reference_on_the_property() -> None:
    """The burial that ``_ref_at_top_level`` exists to undo, asserted
    against a REAL shipped field rather than a fixture model.

    ``secret`` is the field worth pinning: it is optional and templated, so
    pydantic emits it as ``anyOf: [string, null]`` with the marker inside
    the string branch, and the lift hoists it onto the property. The
    fixture-model pins cover both burial shapes, but a fixture cannot
    drift out from under the shipped models. This reddens if a real
    stored arm's ``secret`` stops answering "does this field name a secret?"
    at the property, which is where an editor hover and every consumer
    look.

    Three separate facts, because dropping any one of them would let a
    regression through: the property is branchy (so the marker got there
    by the lift and not by sitting flat), the marker is on the property,
    and no branch kept a copy (lifted, not duplicated, so there stays
    exactly one place to read it).
    """
    stored = document_schema("git-credential")["$defs"]["StoredToken"]
    table = next(branch for branch in stored["anyOf"] if branch.get("type") == "object")
    prop = table["properties"]["secret"]
    assert "anyOf" in prop
    assert prop.get(REF_SCHEMA_KEY, {}).get("kind") == "secret"
    assert all(REF_SCHEMA_KEY not in branch for branch in prop["anyOf"])


# -- Writing, and the modeline ---------------------------------------------


def test_write_schema_set_writes_readable_json_the_loader_ignores(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    written = write_schema_set(resources / SCHEMA_DIRNAME)
    assert {path.name for path in written} == set(schema_set())
    for path in written:
        Draft202012Validator.check_schema(json.loads(path.read_text()))
    # A generated artifact is never read as a declaration. Two independent
    # reasons in ``loader._iter_manifest_files``, and this assertion pins
    # only the second: the walk prunes dot-names (``SCHEMA_DIRNAME`` is
    # dot-prefixed on purpose) AND it takes only ``.yaml`` / ``.yml``.
    # Spelling the directory without its dot fails nothing in this suite,
    # because the suffix filter alone still excludes every ``.schema.json``.
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
