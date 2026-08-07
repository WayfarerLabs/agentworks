"""Tests for ``iter_field_docs``, ``model_doc``, and ``render_type``.

The last test in this file is the anti-drift pin: the same marker facts
have to be readable off the stream and off emitted JSON Schema, since
those are two derivations from one authored marker.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal

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
    render_type,
)
from tests._emitted_schema import ref_extension

from ._fixture_models import (
    AzureLike,
    CatalogLike,
    DiamondLike,
    FieldDiscriminatedSite,
    FrameworkFielded,
    GithubLike,
    LimaArm,
    MultiArmMarked,
    NeverResolved,
    NumericallyTaggedSite,
    OptionalUnionSite,
    RenamedArmSite,
    ResolvesToUnbuildable,
    SelfReferential,
    SiteLike,
    TemplateLike,
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


def test_a_root_model_streams_its_one_root_field() -> None:
    from ._fixture_models import MappingRoot, StringRoot

    assert paths(StringRoot) == [("root",)]
    assert paths(MappingRoot) == [("root",), ("root", "token"), ("root", "api_url")]


def test_an_unbuildable_model_fails_loudly() -> None:
    # The opposite of what extraction does with the same model, and
    # deliberately: a silently truncated field reference is worse than a
    # loud failure in a renderer.
    class Unresolvable(AgwModel):
        nested: NeverDefined  # type: ignore[name-defined]  # noqa: F821

    with pytest.raises(StateError) as exc:
        list(iter_field_docs(Unresolvable))
    assert "Unresolvable" in str(exc.value)


@pytest.mark.parametrize("model_cls", [NeverResolved, ResolvesToUnbuildable])
def test_every_unbuildable_model_fails_as_a_state_error(model_cls: type[AgwModel]) -> None:
    # Including the one whose annotation RESOLVES to something pydantic
    # cannot build: a raw pydantic error escaping here would contradict
    # this walker's own docstring.
    with pytest.raises(StateError):
        list(iter_field_docs(model_cls))


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
    operator as a describe line listing no values and a generated sample
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
    assert dict(docs(Constrained)[("name",)].constraints) == {"min_length": 1, "pattern": "^[a-z]+$"}
    assert dict(docs(Constrained)[("cpus",)].constraints) == {"ge": 1, "le": 64}


def test_no_annotated_types_object_leaks_into_the_record() -> None:
    for doc in iter_field_docs(Constrained):
        for value in doc.constraints.values():
            assert isinstance(value, int | str | float)


# --- authored examples ------------------------------------------------


class Exemplified(AgwModel):
    """A model whose author wrote example values."""

    host: str = Field(examples=["me@gpu-box"])
    sizes: list[str] = Field(default_factory=list, examples=[["small", "large"]])
    plain: str = "unremarkable"


def test_examples_reach_the_stream_in_the_shape_a_document_carries() -> None:
    """A list example stays a list: the generated sample writes the value
    into YAML, so an example spelled as anything else would render as
    something the loader then refuses."""
    assert docs(Exemplified)[("host",)].examples == ("me@gpu-box",)
    assert docs(Exemplified)[("sizes",)].examples == (["small", "large"],)


def test_a_field_with_no_authored_example_reports_none() -> None:
    assert docs(Exemplified)[("plain",)].examples == ()


def test_the_stream_and_the_emitted_schema_report_the_same_examples() -> None:
    """One declaration, two derivations, as with the reference marker: the
    value a sample writes is the value an editor offers."""
    emitted = Exemplified.model_json_schema()["properties"]["host"]["examples"]
    assert list(docs(Exemplified)[("host",)].examples) == emitted


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
    assert docs(GithubLike)[("token",)].annotation is str
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
    they arrive at ``describe`` spelled identically, so an operator reads
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


def test_the_stream_and_the_emitted_schema_agree_inside_a_nested_block() -> None:
    stream = docs(AzureLike)[("service_principal", "secret")]
    assert stream.ref is not None
    emitted = ref_extension(AzureLike.model_json_schema()["$defs"]["PrincipalLike"]["properties"]["secret"])
    assert stream.ref.schema_extension() == emitted


# --- framework fields are not operator surface ------------------------


def test_a_skipped_field_is_not_in_the_stream() -> None:
    """``SkipJsonSchema`` says "framework, not operator" once, for both
    derivations. Without the skip, every rendered sample and every
    describe view would list ``origin`` and ``declared_at`` as fields an
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
