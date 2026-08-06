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

Coverage as of this file's writing: validation, reference extraction, and
the ordered field-reference stream that the rendered sample and the
describe surface both read (``iter_field_docs``). The sample RENDERER and
the describe COMMAND are step 2.8's and do not exist yet; the emitted
JSON Schema is step 2.7's. Those arms belong here as they land, against
this same fixture, rather than in files of their own.
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
