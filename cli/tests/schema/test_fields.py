"""Tests for ``iter_field_docs``, ``model_doc``, and ``render_type``.

The last test in this file is the anti-drift pin: the same marker facts
have to be readable off the stream and off emitted JSON Schema, since
those are two derivations from one authored marker.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Literal

import pytest
from pydantic import AfterValidator, Field, StringConstraints

from agentworks.errors import StateError
from agentworks.schema import (
    MAPPING_KEY,
    SEQUENCE_ELEMENT,
    UNSET,
    AgwModel,
    FieldDoc,
    SecretRef,
    iter_field_docs,
    model_doc,
    model_is_complete,
    render_type,
)
from tests._emitted_schema import ref_extension

if TYPE_CHECKING:
    from collections.abc import Iterator

from ._fixture_models import (
    ALL_FIXTURES,
    AzureLike,
    CatalogLike,
    CredsLike,
    DiamondLike,
    FieldDiscriminatedSite,
    FieldTaggedCollectionSite,
    FrameworkFielded,
    GithubLike,
    LimaArm,
    MultiArmMarked,
    NeverResolved,
    NumericallyTaggedSite,
    OptionalUnionSite,
    RenamedArmSite,
    ResolvesToUnbuildable,
    ScalarOrBlockLike,
    SelfReferential,
    SelfReferentialUnion,
    SiteLike,
    TaggedCollectionSite,
    TemplateLike,
    UndiscriminatedSite,
)


class Arch(Enum):
    """A closed catalog spelled as an enum."""

    ARM64 = "arm64"
    X86_64 = "x86_64"


class Constrained(AgwModel):
    """A model carrying every constraint kind a presenter reports."""

    name: str = Field(min_length=1, pattern="^[a-z]+$")
    cpus: int = Field(default=2, ge=1, le=64)
    mode: Literal["fast", "safe"] = "safe"
    arch: Arch | None = None
    nickname: str | None = None
    tags: list[str] = Field(default_factory=list)


def docs(model_cls: type[AgwModel]) -> dict[tuple[str, ...], FieldDoc]:
    return {doc.path: doc for doc in iter_field_docs(model_cls)}


def paths(model_cls: type[AgwModel]) -> list[tuple[str, ...]]:
    return [doc.path for doc in iter_field_docs(model_cls)]


# --- order and shape of the stream ------------------------------------


def test_fields_come_in_declaration_order() -> None:
    assert paths(Constrained) == [("name",), ("cpus",), ("mode",), ("arch",), ("nickname",), ("tags",)]


def test_a_nested_block_expands_inline_depth_first() -> None:
    assert paths(AzureLike) == [
        ("region",),
        ("service_principal",),
        ("service_principal", "client_id"),
        ("service_principal", "tenant_id"),
        ("service_principal", "secret"),
    ]


def test_the_nested_field_itself_names_the_model_it_opens() -> None:
    from ._fixture_models import PrincipalLike

    assert docs(AzureLike)[("service_principal",)].nested_model is PrincipalLike


def test_two_sibling_fields_of_one_nested_model_both_expand() -> None:
    # An accumulating visited set would render ``primary``'s block and
    # emit nothing for ``fallback``, so the generated sample would be
    # missing a whole section an operator has to write.
    assert paths(DiamondLike) == [
        ("primary",),
        ("primary", "secret"),
        ("fallback",),
        ("fallback", "secret"),
    ]


def test_a_self_referential_model_terminates() -> None:
    # Both routes back to itself stop expanding: the direct field and the
    # one through a collection.
    assert paths(SelfReferential) == [("secret",), ("child",), ("children",)]


def test_a_collection_of_models_expands_under_a_placeholder_segment() -> None:
    # A model says a list holds tables without saying how many, so the
    # element is streamed once. Leaving it out is what made a catalog
    # field render as an opaque "list" in a generated sample.
    assert paths(CatalogLike) == [
        ("vm_sizes",),
        ("vm_sizes", SEQUENCE_ELEMENT, "cpus"),
        ("vm_sizes", SEQUENCE_ELEMENT, "memory"),
        ("vm_sizes", SEQUENCE_ELEMENT, "size"),
        ("accounts",),
        ("accounts", SEQUENCE_ELEMENT, "secret"),
        ("accounts_by_name",),
        ("accounts_by_name", MAPPING_KEY, "secret"),
        ("extra_secrets",),
        ("templates",),
    ]


def test_a_collection_field_names_the_model_its_elements_hold() -> None:
    from ._fixture_models import CatalogEntryLike

    assert docs(CatalogLike)[("vm_sizes",)].item_model is CatalogEntryLike
    assert docs(CatalogLike)[("vm_sizes",)].nested_model is None
    # A collection of NAMES holds no model, and says so.
    assert docs(CatalogLike)[("extra_secrets",)].item_model is None
    assert docs(CatalogLike)[("extra_secrets",)].ref is not None


@pytest.mark.parametrize("platform", ["azure", "aws"])
def test_the_shipped_optional_catalog_shape_expands_its_element(platform: str) -> None:
    """The two real catalog fields, which no test reached.

    ``CatalogLike`` above claims to stand in for them, and on the part
    that matters here it does not: it spells ``list[X] =
    Field(default_factory=list)``, while both shipped fields are
    ``Annotated[list[X], Field(min_length=1)] | None = None``. The walker
    has to peel the optional AND the ``Annotated`` wrapper, in that
    nesting, before it can see a collection at all
    (``schema/_shape.py:198-199``). Miss either peel and the field reads
    as an opaque scalar: no element, so a generated sample and a
    explain listing both stop telling an operator what goes inside
    a catalog entry, with nothing raised.

    Asserted against the element model's OWN field set rather than a list
    of names, so adding a field to a catalog entry does not need this test
    edited to keep meaning something.

    The plugin models are imported inside the test because this package
    is a leaf that must not reach into them (``test_package_boundary``);
    the rule is about the package, and keeping its tests visibly narrow
    too costs nothing.
    """
    from agentworks.plugins.aws.config import AwsEC2Config, AwsInstanceType
    from agentworks.plugins.azure.config import AzureVMConfig, AzureVMSize

    catalogs: dict[str, tuple[type[AgwModel], type[AgwModel], str]] = {
        "azure": (AzureVMConfig, AzureVMSize, "vm_sizes"),
        "aws": (AwsEC2Config, AwsInstanceType, "instance_types"),
    }
    config, element, name = catalogs[platform]

    field_docs = docs(config)

    assert field_docs[(name,)].item_model is element
    # ``| None = None``: optional, and the absent catalog is the default.
    assert field_docs[(name,)].required is False
    assert field_docs[(name,)].default is None
    reached = {path[-1] for path in field_docs if path[:2] == (name, SEQUENCE_ELEMENT)}
    assert reached == set(element.model_fields)


def test_a_root_model_streams_its_one_root_field() -> None:
    from ._fixture_models import MappingRoot, StringRoot

    assert paths(StringRoot) == [("root",)]
    assert paths(MappingRoot) == [("root",), ("root", "token"), ("root", "api_url")]


@pytest.mark.parametrize("model_cls", [NeverResolved, ResolvesToUnbuildable])
def test_every_unbuildable_model_fails_loudly_and_names_itself(model_cls: type[AgwModel]) -> None:
    # The opposite of what extraction does with the same model, and
    # deliberately: a silently truncated field reference is worse than a
    # loud failure in a renderer. Both failures are covered, including the
    # one whose annotation RESOLVES to something pydantic cannot build: a
    # raw pydantic error escaping here would contradict this walker's own
    # docstring, and would not name the model either.
    with pytest.raises(StateError) as exc:
        list(iter_field_docs(model_cls))
    assert model_cls.__name__ in str(exc.value)


# --- required, defaults, descriptions ---------------------------------


def test_a_required_field_reports_no_default() -> None:
    doc = docs(Constrained)[("name",)]
    assert doc.required is True
    assert doc.default is UNSET


def test_a_declared_default_is_reported() -> None:
    assert docs(Constrained)[("cpus",)].default == 2


def test_a_declared_default_of_none_is_not_the_absence_of_one() -> None:
    doc = docs(Constrained)[("nickname",)]
    assert doc.required is False
    assert doc.default is None


def test_a_default_factory_is_called() -> None:
    assert docs(Constrained)[("tags",)].default == []


def test_a_factory_that_needs_the_other_fields_reports_no_default() -> None:
    """Pydantic lets a ``default_factory`` take the already-validated
    values of the fields declared before it. A doc walker has none: it is
    describing the shape, not validating a document, so there is nothing
    to hand such a factory and calling it would raise.

    Reporting UNSET is the honest answer, and it is the same one a
    required field gets, which is exactly right for the surfaces
    downstream: the sample renderer writes a live line only for a field
    whose value it knows, so this field is offered as one the operator
    fills in rather than pre-filled with a value invented from an empty
    document.
    """

    class Derived(AgwModel):
        prefix: str = "agw"
        label: str = Field(default_factory=lambda data: f"{data['prefix']}-label")

    doc = docs(Derived)[("label",)]

    assert doc.default is UNSET
    # The factory really is the validated-data kind, and it really does
    # work when pydantic (not the walker) calls it: the walker is
    # declining to invent an input, not routing around a broken factory.
    assert Derived().label == "agw-label"


def test_the_description_comes_from_the_attribute_docstring() -> None:
    class Documented(AgwModel):
        vm_host: str | None = None
        """SSH host running limactl for a remote-Lima site. Omit for a local site."""

        undocumented: str = "x"

    assert docs(Documented)[("vm_host",)].description == (
        "SSH host running limactl for a remote-Lima site. Omit for a local site."
    )
    # Not an error at this layer; it renders as an undocumented field.
    assert docs(Documented)[("undocumented",)].description is None


def test_a_models_own_description_is_the_first_docstring_paragraph() -> None:
    class Paragraphs(AgwModel):
        """The summary line,
        wrapped across two source lines.

        A second paragraph a heading has no room for.
        """

    assert model_doc(GithubLike).description == "A token-sourcing git-credential provider's config."
    assert model_doc(Paragraphs).description == "The summary line, wrapped across two source lines."


def test_a_model_with_no_docstring_reports_no_description() -> None:
    class Bare(AgwModel):
        name: str = "x"

    assert model_doc(Bare).description is None
    assert model_doc(Bare).title == "Bare"


# --- choices and constraints ------------------------------------------


def test_a_literal_field_reports_its_choices() -> None:
    assert docs(Constrained)[("mode",)].choices == ("fast", "safe")


def test_an_enum_field_reports_its_members() -> None:
    assert docs(Constrained)[("arch",)].choices == (Arch.ARM64, Arch.X86_64)


def test_a_discriminator_is_itself_a_literal_field() -> None:
    assert docs(LimaArm)[("name",)].choices == ("lima",)


def test_an_open_field_reports_no_choices() -> None:
    assert docs(Constrained)[("nickname",)].choices == ()


@pytest.mark.parametrize(
    ("field_name", "expected"),
    [
        ("wrapper_in_the_union", ("x", "y")),
        ("union_in_the_wrapper", ("x", "y")),
        ("validated_enum", (Arch.ARM64, Arch.X86_64)),
        ("described_enum", (Arch.ARM64, Arch.X86_64)),
        ("bare_optional", ("x", "y")),
        ("bare", ("x", "y")),
    ],
)
def test_choices_survive_every_annotated_and_optional_spelling(field_name: str, expected: tuple[object, ...]) -> None:
    """A closed field stays closed however its author nested ``Annotated``
    and ``| None``.

    Pydantic keeps the spelling that was written, so the annotation on the
    record is ``Optional[Annotated[Literal[...], ...]]`` for one field and
    ``Optional[Literal[...]]`` for the next, and a reader that peeled only
    one wrapper reported an OPEN field for a closed one. That reaches an
    operator as an explain line listing no values and a generated sample
    with a placeholder instead of a real one: wrong, and with nothing to
    signal that it is.
    """

    def keep(value: object) -> object:
        return value

    class Spellings(AgwModel):
        wrapper_in_the_union: Annotated[Literal["x", "y"], Field(description="d")] | None = None
        union_in_the_wrapper: Annotated[Literal["x", "y"] | None, Field(description="d")] = None
        validated_enum: Annotated[Arch, AfterValidator(keep)] | None = None
        described_enum: Annotated[Arch, Field(description="d")] | None = None
        bare_optional: Literal["x", "y"] | None = None
        bare: Literal["x", "y"] = "x"

    assert docs(Spellings)[(field_name,)].choices == expected


def test_constraints_are_normalized_to_plain_keys_and_values() -> None:
    # Plain values as well as plain keys: an ``annotated_types`` object
    # leaking through would compare unequal to the bare bound here, so
    # nobody downstream has to know pydantic stores them wrapped.
    assert dict(docs(Constrained)[("name",)].constraints) == {"min_length": 1, "pattern": "^[a-z]+$"}
    assert dict(docs(Constrained)[("cpus",)].constraints) == {"ge": 1, "le": 64}


# --- authored examples ------------------------------------------------


class Exemplified(AgwModel):
    """A model whose author wrote example values."""

    host: str = Field(examples=["me@gpu-box"])
    sizes: list[str] = Field(default_factory=list, examples=[["small", "large"]])
    plain: str = "unremarkable"


def test_examples_reach_the_stream_in_the_shape_a_document_carries() -> None:
    """A list example stays a list: the generated sample writes the value
    into YAML, so an example spelled as anything else would render as
    something the loader then refuses.

    That verbatim shape is also what keeps the two derivations agreeing:
    emitted JSON Schema carries the authored values unchanged, so the
    value a sample writes is the value an editor offers.
    """
    assert docs(Exemplified)[("host",)].examples == ("me@gpu-box",)
    assert docs(Exemplified)[("sizes",)].examples == (["small", "large"],)
    assert Exemplified.model_json_schema()["properties"]["host"]["examples"] == ["me@gpu-box"]


def test_a_field_with_no_authored_example_reports_none() -> None:
    assert docs(Exemplified)[("plain",)].examples == ()


# --- reference semantics ----------------------------------------------


def test_a_templated_field_is_required_with_no_default() -> None:
    # The template is NOT a default: it is a name the model resolves at
    # validation from the owner, so the field stays required and reports
    # no default. This pair is what an `after`-mode fill would have
    # corrupted (it can only fill a field that already carries a
    # placeholder default), so it pins the mechanism, not just the value.
    doc = docs(GithubLike)[("token",)]
    assert doc.required is True
    assert doc.default is UNSET
    assert doc.default_template == "git-token-{owner_name}"


def test_a_marked_field_carries_its_marker_verbatim() -> None:
    doc = docs(GithubLike)[("token",)]
    assert doc.ref is not None
    assert doc.ref.kind == "secret"
    assert doc.ref.usage == "the auth token"
    assert doc.default_template == "git-token-{owner_name}"


def test_a_marked_list_carries_the_element_marker_and_no_default_identity() -> None:
    doc = docs(TemplateLike)[("inherits",)]
    assert doc.ref is not None
    assert doc.ref.kind == "vm-template"
    assert doc.default_template is None


def test_the_annotation_has_its_markers_stripped() -> None:
    """The marker itself never reaches the annotation. Its EFFECT on what
    an operator may write does: an owner-templated field reads an explicit
    ``null`` as the omission it resolves, so the annotation says ``null``
    is accepted, exactly as the emitted schema does. A marked LIST cannot
    carry a template, so nothing is added there."""
    assert docs(GithubLike)[("token",)].annotation == (str | None)
    assert docs(TemplateLike)[("inherits",)].annotation == list[str]
    assert docs(TemplateLike)[("image",)].annotation == (str | None)


# --- union arms -------------------------------------------------------


def test_union_arms_are_handles_rather_than_expanded_fields() -> None:
    doc = docs(SiteLike)[("platform",)]
    assert [arm.tag for arm in doc.union_arms] == ["lima", "proxmox"]
    # The arms' own fields are NOT in the stream: the presenter decides
    # whether to render one arm, all of them, or a table.
    assert paths(SiteLike) == [("platform",)]


@pytest.mark.parametrize("model_cls", [SiteLike, FieldDiscriminatedSite, OptionalUnionSite])
def test_every_legal_union_spelling_yields_the_same_arms(model_cls: type[AgwModel]) -> None:
    assert [arm.tag for arm in docs(model_cls)[("platform",)].union_arms] == ["lima", "proxmox"]


def test_an_arm_answering_to_two_tags_is_listed_under_both() -> None:
    arms = docs(RenamedArmSite)[("platform",)].union_arms
    assert [arm.tag for arm in arms] == ["lima", "aws-ec2", "ec2"]
    assert arms[1].doc.model is arms[2].doc.model


def test_a_union_tagged_by_a_non_name_lists_no_arms() -> None:
    assert docs(NumericallyTaggedSite)[("thing",)].union_arms == ()


def test_a_marker_inside_a_multi_arm_union_is_still_reported() -> None:
    doc = docs(MultiArmMarked)[("secret",)]
    assert doc.ref is not None
    assert doc.default_template == "multi-arm-secret"


@pytest.mark.parametrize(
    ("field_name", "expected"),
    [
        ("outside", {"min_length": 2}),
        ("in_a_list", {"min_length": 3}),
        ("plain", {"min_length": 4}),
    ],
)
def test_constraints_are_found_in_every_spelling(field_name: str, expected: dict[str, object]) -> None:
    # Same lookup asymmetry as the union spellings: pydantic accepts all
    # three, so a presenter must not see a field as unconstrained just
    # because of where the author put the ``Field``.
    class Spellings(AgwModel):
        outside: Annotated[str, Field(min_length=2)] | None = None
        in_a_list: list[Annotated[str, Field(min_length=3)]] = Field(default_factory=list)
        plain: str = Field(default="xxxx", min_length=4)

    assert dict(docs(Spellings)[(field_name,)].constraints) == expected


def test_a_collection_reports_one_carriers_constraints_not_a_mixture() -> None:
    """A list's ``min_length`` bounds how many entries it holds; a string
    element's bounds how long one entry is. Merged into a single mapping
    they arrive at ``explain`` spelled identically, so an operator reads
    a limit on the wrong thing and there is no way to tell.

    The field's own spine wins whole, matching what ``ref`` does with a
    marker (``shape.marker or shape.item_marker``): what the author
    spelled on the field itself, and the elements' only when the field
    says nothing. Reporting one carrier can omit a fact; reporting a mix
    states one that is false.
    """

    class Bounded(AgwModel):
        both: Annotated[
            list[Annotated[str, Field(min_length=3)]],
            Field(min_length=1, max_length=9),
        ] = Field(default_factory=list)
        elements_only: list[Annotated[str, Field(min_length=3)]] = Field(default_factory=list)
        spine_only: list[str] = Field(default_factory=list, min_length=1)

    entries = docs(Bounded)
    assert dict(entries[("both",)].constraints) == {"min_length": 1, "max_length": 9}
    assert dict(entries[("elements_only",)].constraints) == {"min_length": 3}
    assert dict(entries[("spine_only",)].constraints) == {"min_length": 1}


def test_each_arm_carries_its_own_identity() -> None:
    arms = {arm.tag: arm.doc for arm in docs(SiteLike)[("platform",)].union_arms}
    assert arms["lima"].title == "LimaArm"
    assert arms["lima"].description == "The union arm that names no Resource."


def test_an_arm_is_recursed_into_by_the_presenter_not_the_walker() -> None:
    arm = next(arm for arm in docs(SiteLike)[("platform",)].union_arms if arm.tag == "proxmox")
    assert paths(arm.doc.model) == [("name",), ("token_secret",)]


# --- a collection whose ELEMENTS are a union --------------------------


def test_a_collection_of_tagged_blocks_carries_its_element_arms() -> None:
    """The arms ride one level down, where the elements are.

    Read off ``union_arms``, a presenter would render the collection
    ITSELF as one arm: the arm's fields would land beside the collection
    rather than inside an element of it, which in a document is a
    different shape entirely.
    """
    doc = docs(TaggedCollectionSite)[("platforms",)]

    assert [arm.tag for arm in doc.item_union_arms] == ["lima", "proxmox"]
    assert doc.union_arms == ()
    # Not a collection of ONE model, so there is no block to expand under
    # the placeholder: which arm's fields to show is the presenter's.
    assert doc.item_model is None
    assert paths(TaggedCollectionSite) == [("platforms",), ("platforms_by_name",)]


@pytest.mark.parametrize("model_cls", [TaggedCollectionSite, FieldTaggedCollectionSite])
def test_every_element_tag_spelling_yields_the_same_arms(model_cls: type[AgwModel]) -> None:
    # The same lookup asymmetry as the union spellings one level up:
    # pydantic accepts both, so a stream that read only one would describe
    # a shape it can validate as an opaque list of tables.
    assert [arm.tag for arm in docs(model_cls)[("platforms",)].item_union_arms] == ["lima", "proxmox"]


def test_each_element_arm_carries_its_own_identity() -> None:
    arms = {arm.tag: arm.doc for arm in docs(TaggedCollectionSite)[("platforms",)].item_union_arms}

    assert arms["proxmox"].title == "ProxmoxArm"
    assert arms["proxmox"].description == "The union arm that names a secret."


def test_the_element_segment_says_how_an_element_is_addressed() -> None:
    """A presenter has to place an element before it can render one, and
    the model is what knows whether it hangs under a position or a key."""
    tagged = docs(TaggedCollectionSite)

    assert tagged[("platforms",)].item_segment == SEQUENCE_ELEMENT
    assert tagged[("platforms_by_name",)].item_segment == MAPPING_KEY
    # Every collection reports it, whatever its elements are, and a field
    # holding a single value reports none.
    assert docs(CatalogLike)[("templates",)].item_segment == SEQUENCE_ELEMENT
    assert docs(CatalogLike)[("extra_secrets",)].item_segment == MAPPING_KEY
    assert docs(SiteLike)[("platform",)].item_segment is None


def test_the_element_segment_is_the_one_the_stream_hangs_fields_under() -> None:
    # One fact, not two: a collection of plain blocks streams its
    # element's fields under exactly the segment the doc names, so a
    # presenter placing a node and the stream placing a field cannot
    # disagree about where an element lives.
    doc = docs(CatalogLike)[("vm_sizes",)]

    assert doc.item_segment is not None
    assert ("vm_sizes", doc.item_segment, "cpus") in paths(CatalogLike)


# --- a union of scalars and ONE block ---------------------------------


def test_a_scalar_or_block_union_opens_the_block_it_offers() -> None:
    """The one shape an undiscriminated union CAN address: a union of
    scalars and one model offers exactly one block, so the block's fields
    follow it in the stream rather than the field reading as an opaque
    "table" beside a schema that spells its properties out.
    """
    doc = docs(ScalarOrBlockLike)[("mapping",)]

    assert doc.nested_model is CredsLike
    assert render_type(doc.annotation) == "string or table or null"
    assert ("mapping", "secret") in paths(ScalarOrBlockLike)


def test_a_collection_of_scalar_or_block_elements_opens_its_element() -> None:
    """The same shape one level down, which no shipped field has and any
    capability or plugin author can write."""
    stream = docs(ScalarOrBlockLike)
    walked = paths(ScalarOrBlockLike)

    assert stream[("mappings",)].item_model is CredsLike
    assert stream[("mapping_list",)].item_model is CredsLike
    assert ("mappings", MAPPING_KEY, "secret") in walked
    assert ("mapping_list", SEQUENCE_ELEMENT, "secret") in walked


def test_a_union_of_several_models_still_opens_nothing() -> None:
    """Two model members and no tag: nothing addresses an arm from a raw
    blob, so naming one would be a guess and rendering one would tell an
    operator that the arm the walk met first is the one to write."""
    doc = docs(UndiscriminatedSite)[("platform",)]

    assert doc.nested_model is None
    assert paths(UndiscriminatedSite) == [("platform",)]


def test_a_block_a_union_offers_stops_when_it_is_reachable_from_itself() -> None:
    """The stream's own path guard covers the block a union offers, the
    same way it covers one a field opens outright: the model is already on
    the current path, so the field is streamed and not descended into."""
    # The same answer the shape gets when the block is opened outright
    # (``test_a_self_referential_model_terminates``), which is what says
    # the guard is one guard rather than two.
    assert paths(SelfReferentialUnion) == [("name",), ("child",)]


# --- render_type ------------------------------------------------------


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (str, "string"),
        (int, "integer"),
        (float, "number"),
        (bool, "boolean"),
        (str | None, "string or null"),
        (list[str], "list of string"),
        (list[int] | None, "list of integer or null"),
        (dict[str, str], "table of string"),
        (Literal["fast", "safe"], "one of: fast, safe"),
        (Arch, "one of: arm64, x86_64"),
        (LimaArm, "table"),
        (Annotated[str, SecretRef(usage="u")], "string"),
        # A constraint is not a type an operator writes, so the Annotated
        # wrapper it rides in is dropped rather than named. Before this,
        # a constrained field rendered as the literal word "Annotated".
        (Annotated[str, StringConstraints(min_length=1)], "string"),
        (Annotated[str, StringConstraints(min_length=1)] | None, "string or null"),
        (list[Annotated[str, StringConstraints(pattern="^a")]], "list of string"),
        # What the operator writes for a moment in time, not the class
        # validation produces.
        (datetime, "timestamp"),
        (date, "date"),
    ],
)
def test_render_type(annotation: object, expected: str) -> None:
    assert render_type(annotation) == expected


def test_a_union_of_models_renders_once() -> None:
    assert render_type(docs(SiteLike)[("platform",)].annotation) == "table"


# --- the anti-drift pin -----------------------------------------------


def test_the_stream_and_the_emitted_schema_report_the_same_marker() -> None:
    # Two derivations, one authored marker. If these can disagree, the
    # sample renderer and an operator's editor tooling can disagree.
    stream = docs(GithubLike)[("token",)]
    assert stream.ref is not None
    assert stream.ref.schema_extension() == ref_extension(GithubLike.model_json_schema()["properties"]["token"])


def test_the_stream_offers_every_arm_the_emitted_schema_does() -> None:
    """No arm a document may name is missing from the field stream, at
    either depth, for any model shape.

    The oracle is the SIBLING derivation: pydantic writes a
    ``discriminator.mapping`` naming every tag it will dispatch on, and it
    does so without consulting anything in this package. An arm in the
    mapping and absent from the stream is a value an editor offers, the
    loader accepts, and ``explain`` never mentions, which is how a
    collection of tagged blocks read as an opaque list of tables for as
    long as it did.

    Equality, not containment: a stream naming an arm nothing dispatches
    on would send an operator to write a document the loader refuses.

    Every buildable fixture in one sweep, reporting EVERY disagreement
    rather than one model per case. The two derivations move together, so
    a change that breaks the correspondence breaks it for a whole class of
    shapes at once, and the useful failure is the list of them: a case per
    model tells you the first that broke and hides the other twelve.
    """
    disagreements = [
        report for model_cls in ALL_FIXTURES if model_is_complete(model_cls) for report in _arm_disagreements(model_cls)
    ]
    assert disagreements == [], "the stream and the emitted schema offer different arms:\n" + "\n".join(disagreements)


def _arm_disagreements(model_cls: type[AgwModel]) -> Iterator[str]:
    """Every way ``model_cls``'s stream and emitted schema disagree about
    what a document may name, each addressed by model and field."""
    emitted = model_cls.model_json_schema()
    stream = docs(model_cls)
    for name, prop in emitted.get("properties", {}).items():
        where = f"  {model_cls.__name__}.{name}"
        doc = stream.get((name,))
        if doc is None:
            yield f"{where}: in the emitted schema and not in the stream"
            continue
        field = _peeled_schema(prop)
        for depth, offered, streamed in (
            ("the field", _tags_offered(field, emitted), {arm.tag for arm in doc.union_arms}),
            ("one element", _tags_offered(_element_schema(field), emitted), {arm.tag for arm in doc.item_union_arms}),
        ):
            if offered != streamed:
                ordered_streamed = sorted(streamed, key=lambda tag: "" if tag is None else tag)
                yield (f"{where}: {depth} dispatches on {sorted(offered)} and the stream offers {ordered_streamed}")


def _peeled_schema(schema: dict[str, object]) -> dict[str, object]:
    """A property's schema with the ``| None`` wrapper removed.

    Pydantic spells an optional field as an ``anyOf`` of the type and
    ``null``, so the union under it is one level down.
    """
    members = schema.get("anyOf")
    if not isinstance(members, list):
        return schema
    present = [member for member in members if member != {"type": "null"}]
    return present[0] if len(present) == 1 and isinstance(present[0], dict) else schema


def _element_schema(schema: dict[str, object]) -> dict[str, object] | None:
    """What ONE element of a collection property validates against."""
    for key in ("items", "additionalProperties"):
        element = schema.get(key)
        if isinstance(element, dict):
            return element
    return None


def _tags_offered(schema: dict[str, object] | None, emitted: dict[str, object]) -> set[str]:
    """Every tag ``schema``'s discriminated union dispatches on, as a
    DOCUMENT spells it.

    A union tagged by something other than a name is excluded rather than
    stringified: JSON Schema keys a numeric tag as ``"1"``, an operator
    writes ``1``, and the stream declines to address such an arm at all
    (see :func:`agentworks.schema._shape._tags_of`).
    """
    discriminator = schema.get("discriminator") if schema else None
    if not isinstance(discriminator, dict):
        return set()
    mapping = discriminator["mapping"]
    return {tag for tag, ref in mapping.items() if _tag_type(ref, discriminator["propertyName"], emitted) == "string"}


def _tag_type(ref: str, tag_field: str, emitted: dict[str, object]) -> object:
    """The JSON type of one arm's discriminator field."""
    definitions = emitted["$defs"]
    assert isinstance(definitions, dict)
    arm = definitions[ref.removeprefix("#/$defs/")]
    return arm["properties"][tag_field].get("type")


def test_the_stream_and_the_emitted_schema_agree_inside_a_nested_block() -> None:
    stream = docs(AzureLike)[("service_principal", "secret")]
    assert stream.ref is not None
    emitted = ref_extension(AzureLike.model_json_schema()["$defs"]["PrincipalLike"]["properties"]["secret"])
    assert stream.ref.schema_extension() == emitted


# --- framework fields are not operator surface ------------------------


def test_a_skipped_field_is_not_in_the_stream() -> None:
    """``SkipJsonSchema`` says "framework, not operator" once, for both
    derivations. Without the skip, every rendered sample and every
    explain view would list ``origin`` and ``declared_at`` as fields an
    operator should fill in."""
    assert [doc.path for doc in iter_field_docs(FrameworkFielded)] == [("name",), ("cpus",)]


def test_a_skipped_field_is_not_in_the_emitted_schema_either() -> None:
    """The other half of the same claim, pinned beside it: the two
    derivations drop the same set, off the same marker."""
    emitted = FrameworkFielded.model_json_schema()

    assert set(emitted["properties"]) == {"name", "cpus"}
    assert emitted["additionalProperties"] is False


def test_a_skipped_field_still_validates() -> None:
    """The marker hides the field from the two PRESENTATIONS; it does not
    take it off the model, which is what makes the row and its spec one
    class."""
    row = FrameworkFielded.model_validate({"name": "n", "origin": "operator-declared"})

    assert row.origin == "operator-declared"
