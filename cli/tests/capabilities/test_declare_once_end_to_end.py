"""One declaration, every surface: the FRD's declare-once success
criterion, proven end to end on a fixture capability.

What this file is FOR is what it does NOT contain. The fixture platform
below declares one field, ``region``, with a default and a description,
and registers through the real plugin machinery. Nothing else in the tree
mentions it: no validator registration, no sample entry, no docs table,
no schema hand-off. Every assertion below then reads that one declaration
back off a DIFFERENT derived surface. If a surface ever needs its own
edit to learn about a field, one of these fails, which is the whole
promise (FR15 for the default, FR6 for the description, FR13 for the
regime being provable on a fixture rather than only on shipped models).

Coverage: validation, reference extraction, the emitted JSON Schema, the
ordered field-reference stream both human surfaces read
(``iter_field_docs``), and, since step 2.8, those two surfaces themselves:
the rendered sample and the field reference. Every arm is against this one
fixture rather than in files of its own, where the with-no-other-edits
claim would be split up and weakened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

import pytest

from agentworks.capabilities.config import (
    capability_config_model,
    capability_config_references,
    validate_capability_config,
)
from agentworks.errors import ConfigError
from agentworks.manifests.describe import reference_lines
from agentworks.manifests.emit import document_schema
from agentworks.manifests.reference import reference_for
from agentworks.manifests.samples import sample_text
from agentworks.plugins import Plugin, seated_plugin
from agentworks.schema import AgwModel, NonEmptyStr, RefOwner, SecretRef, iter_field_docs
from tests.plugins._fixtures import ConformingVMPlatform

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.schema import FieldDoc

OWNER = RefOwner(kind="vm-site", name="lab")

#: Spelled once so every assertion below is demonstrably reading the SAME
#: declaration rather than a coincidence, and so a reader can see at a
#: glance that the description text is not repeated anywhere.
REGION_DEFAULT = "westus2"
REGION_DESCRIPTION = "Where this fixture platform creates its VMs."


class DeclareOnceConfig(AgwModel):
    """A fixture platform's config: one defaulted field, one derived one."""

    name: Literal["declare-once"]
    """The platform this config is for."""

    # The field under test. Its default and its description are stated
    # here and nowhere else.
    region: NonEmptyStr = REGION_DEFAULT
    """Where this fixture platform creates its VMs."""

    token: Annotated[
        NonEmptyStr,
        SecretRef(usage="the fixture token", default_template="declare-once-{owner_name}"),
    ]
    """The secret holding this platform's API token."""


class DeclareOncePlatform(ConformingVMPlatform):
    name: ClassVar[str] = "declare-once"
    description: ClassVar[str] = "a fixture platform proving declare-once"
    config_model: ClassVar[type[AgwModel]] = DeclareOnceConfig


@pytest.fixture
def seated() -> Iterator[None]:
    """The fixture platform in the live vm-platform registry, seated
    through the shipped plugin machinery and removed again on the way
    out, failure included."""
    with seated_plugin(Plugin(name="declare-once-fixtures", capabilities={"vm-platform": (DeclareOncePlatform,)})):
        yield


def _validate(blob: dict[str, object]) -> DeclareOnceConfig:
    validated = validate_capability_config(
        kind="vm-platform",
        config={"name": "declare-once", **blob},
        owner=OWNER,
    )
    assert isinstance(validated, DeclareOnceConfig)
    return validated


def _region_doc() -> FieldDoc:
    docs = {doc.path: doc for doc in iter_field_docs(DeclareOnceConfig)}
    return docs[("region",)]


# -- The registry finds the declaration without being told about it ----------


def test_seating_the_capability_is_the_whole_registration(seated: None) -> None:
    """The premise every assertion below rests on: the core reaches the
    model through the registry, so declaring a field is the only step
    there is."""
    assert capability_config_model("vm-platform", "declare-once") is DeclareOnceConfig


# -- Surface 1: validation ----------------------------------------------------


def test_validation_applies_the_declared_default(seated: None) -> None:
    """A document that omits the field validates, and the instance the
    consumer receives carries the value: fully resolved, with nothing left
    for the consumer to fall back to (FR15)."""
    assert _validate({}).region == REGION_DEFAULT


def test_an_explicit_value_wins_over_the_default(seated: None) -> None:
    assert _validate({"region": "eastus"}).region == "eastus"


def test_the_declarations_constraint_reaches_validation(seated: None) -> None:
    """``NonEmptyStr`` is part of the same one declaration, so it is
    enforced without a validator being registered anywhere."""
    with pytest.raises(ConfigError, match="region: must not be empty"):
        _validate({"region": ""})


def test_a_default_is_checked_rather_than_trusted() -> None:
    """``validate_default`` is on, so a default that violates its own
    field's constraint is caught. It fires when a document OMITS the
    field, not at class definition, which is why this model can be
    declared at all."""

    class BadDefaultConfig(AgwModel):
        name: Literal["bad-default"]
        region: NonEmptyStr = ""

    BadDefaultConfig(name="bad-default", region="ok")
    with pytest.raises(ValueError, match="region"):
        BadDefaultConfig(name="bad-default")


# -- Surface 2: reference extraction ------------------------------------------


def test_extraction_derives_the_omitted_reference_from_the_declaration(seated: None) -> None:
    """The DERIVED default (FR15's second kind): the operator writes no
    token, and the graph edge still names the secret the marker's template
    implies, without extraction knowing anything about this capability."""
    refs = capability_config_references(
        kind="vm-platform",
        config={"name": "declare-once"},
        owner=OWNER,
    )

    assert [ref.name for ref in refs] == ["declare-once-lab"]


def test_the_validated_instance_names_the_same_secret_as_the_edge(seated: None) -> None:
    """The two must agree or a consumer reading the field would target a
    different secret than the one the graph resolved."""
    assert _validate({}).token == "declare-once-lab"


# -- Surface 3: the field-reference stream (sample and describe read this) ----


def test_the_stream_carries_the_default_and_the_description() -> None:
    """``iter_field_docs`` is the one ordered stream the rendered sample
    and the describe surface are both built on, so a field reaching it is
    a field reaching both. The description comes from the attribute
    docstring: there is no second place to author it."""
    region = _region_doc()

    assert region.default == REGION_DEFAULT
    assert region.description == REGION_DESCRIPTION
    assert region.required is False


def test_the_stream_carries_the_reference_semantics_too() -> None:
    """The marker rides the same declaration, so a presenter can tell an
    operator what an omitted ``token`` will resolve to."""
    docs = {doc.path: doc for doc in iter_field_docs(DeclareOnceConfig)}
    token = docs[("token",)]

    assert token.default_template == "declare-once-{owner_name}"
    assert token.ref is not None and token.ref.usage == "the fixture token"


def test_the_stream_is_the_declaration_order() -> None:
    """Determinism is part of the contract: a rendered sample has to be
    stable across runs or the tests pinning it are worthless."""
    assert [doc.path for doc in iter_field_docs(DeclareOnceConfig)] == [("name",), ("region",), ("token",)]


# -- Surface 4: the emitted JSON Schema ---------------------------------------


def _emitted_arm() -> dict[str, object]:
    """The fixture platform's arm of the vm-site document schema.

    Emitted for the HOST kind, not for the capability: a schema-aware
    editor validates the manifest an operator writes, and the arm is
    reachable only because the platform is seated.
    """
    schema = document_schema("vm-site")
    defs = schema["$defs"]
    assert isinstance(defs, dict)
    arm = defs["DeclareOnceConfig"]
    assert isinstance(arm, dict)
    return arm


def test_the_emitted_schema_carries_the_default_and_the_description(seated: None) -> None:
    """Same one declaration, reaching the editor: the operator's YAML
    completes ``region`` with its default and hovers its description
    without either being authored a second time."""
    region = _emitted_arm()["properties"]["region"]  # type: ignore[index]

    assert region["default"] == REGION_DEFAULT
    assert region["description"] == REGION_DESCRIPTION
    assert region["minLength"] == 1


def test_a_defaulted_field_is_not_emitted_as_required(seated: None) -> None:
    """The counterpart of applying the default: an editor must not
    red-underline the omission the model resolves. ``token`` is not
    required either, for the derived case, since ``AgwModel`` fills it
    from its owner template."""
    assert _emitted_arm()["required"] == ["name"]


def test_the_arm_is_there_only_because_the_capability_is_seated() -> None:
    """The other half of "no other edits": emission reads the live
    registry, so an unseated platform contributes no arm, and seating one
    is the entire act of publishing its schema."""
    assert "DeclareOnceConfig" not in document_schema("vm-site")["$defs"]


# -- Surface 5: the rendered sample -------------------------------------------


def test_the_rendered_sample_offers_the_capability_as_an_alternative(seated: None) -> None:
    """``agw resource sample vm-site`` renders ONE platform and names the
    rest, so what a seated fixture reaches here is the alternatives line.
    That is the whole edit: no sample file, no kind table, no entry
    anywhere that says this platform exists."""
    text = sample_text("vm-site")

    assert "declare-once" in text
    assert "`agw resource describe-kind vm-platform/" in text


def test_no_sample_mentions_the_capability_when_it_is_not_seated() -> None:
    """The counterpart, and the reason the arm above is not a coincidence:
    the sample is rendered from the live registry rather than from a file
    that happens to list platforms."""
    assert "declare-once" not in sample_text("vm-site")


# -- Surface 6: the field reference -------------------------------------------


def test_the_field_reference_reads_the_same_one_declaration(seated: None) -> None:
    """``agw resource describe-kind vm-platform/declare-once`` documents a
    capability whose field, default, and description are declared once, on
    the model, and nowhere else. Nothing was registered with the describe
    surface; seating the capability is what put it there."""
    reference = reference_for("vm-platform/declare-once")
    region = next(entry for entry in reference.spec if entry.name == "region")

    assert region.doc.default == REGION_DEFAULT
    assert region.doc.description == REGION_DESCRIPTION

    rendered = "\n".join(reference_lines(reference))
    assert f"region  (string, optional, default {REGION_DEFAULT}, min length 1)" in rendered
    assert REGION_DESCRIPTION in rendered


def test_the_field_reference_makes_the_same_templated_subtraction(seated: None) -> None:
    """``token`` is required to pydantic and optional to the operator,
    because the model fills it from its owner. Emitted schema already
    stopped calling it required (above); the human surfaces make the same
    subtraction from the same marker, and say what the omission
    resolves to."""
    rendered = "\n".join(reference_lines(reference_for("vm-platform/declare-once")))

    assert "token  (string, optional, defaults to `declare-once-<name>`" in rendered
