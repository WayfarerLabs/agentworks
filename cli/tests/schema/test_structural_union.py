"""The model-derived contract for untagged structural unions."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

import pytest
from pydantic import AliasChoices, BaseModel, Discriminator, Field

from agentworks.schema import (
    MAPPING_KEY,
    AgwModel,
    RefOwner,
    ScalarShorthand,
    SecretRef,
    StructuralUnion,
    extract_references,
    filled_defaults,
    iter_field_docs,
    reference_marker_error,
    structural_union_error,
)
from agentworks.schema._shape import structural_arm_for

OWNER = RefOwner(kind="fixture", name="demo")


class PlainArm(AgwModel):
    """A plaintext source."""

    scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="value")

    value: str


class SecretArm(AgwModel):
    """A named secret source."""

    secret: Annotated[str, SecretRef(usage="the fixture source")]


Source = Annotated[PlainArm | SecretArm, StructuralUnion()]


class DefaultSecretArm(AgwModel):
    secret: Annotated[
        str,
        SecretRef(usage="the defaulted source", default_template="source-{owner_name}"),
    ]


DefaultSource = Annotated[PlainArm | DefaultSecretArm, StructuralUnion()]


class Holder(AgwModel):
    sources: dict[str, Source] = Field(default_factory=dict)


class OptionalHolder(AgwModel):
    source: Source | None = None


class OptionalItemsHolder(AgwModel):
    sources: list[Source | None] = Field(default_factory=list)


class OptionalDefaultHolder(AgwModel):
    source: DefaultSource | None = None


class OptionalDefaultItemsHolder(AgwModel):
    sources: list[DefaultSource | None] = Field(default_factory=list)


def _names(blob: object) -> list[str]:
    return [ref.name for ref in extract_references(Holder, blob)]


def test_unique_required_and_allowed_keys_select_the_arm() -> None:
    assert _names({"sources": {"plain-short": "text", "plain-long": {"value": "text"}}}) == []
    assert _names({"sources": {"secret": {"secret": "api-token"}}}) == ["api-token"]


def test_malformed_or_ambiguous_tables_do_not_guess_an_edge() -> None:
    assert _names({"sources": {"mixed": {"value": "text", "secret": "api-token"}}}) == []
    assert _names({"sources": {"neither": {}}}) == []
    assert _names({"sources": {"unknown": {"secrit": "api-token"}}}) == []
    assert _names({"sources": {"bad-name": {"secret": 8}}}) == []


def test_structural_unions_reject_foreign_null_keys() -> None:
    blob = {"sources": {"plain": {"value": "text", "secret": None}}}

    with pytest.raises(ValueError):
        Holder.model_validate(blob)
    assert _names(blob) == []

    emitted = Holder.model_json_schema()
    assert set(emitted["$defs"]["PlainArm"]["anyOf"][1]["properties"]) == {"value"}
    assert set(emitted["$defs"]["SecretArm"]["properties"]) == {"secret"}


def test_owner_default_filling_uses_the_same_structural_selection() -> None:
    class DefaultHolder(AgwModel):
        source: DefaultSource

    filled = filled_defaults(DefaultHolder, {"source": {}}, OWNER)

    assert filled == {"source": {"secret": "source-demo"}}
    assert DefaultHolder.model_validate(filled).source == DefaultSecretArm(secret="source-demo")
    assert [ref.name for ref in extract_references(DefaultHolder, filled)] == ["source-demo"]


@pytest.mark.parametrize(
    "model",
    [OptionalHolder, OptionalItemsHolder],
    ids=["optional-field", "optional-collection-element"],
)
def test_optional_structural_unions_are_visible_to_conformance(model: type[AgwModel]) -> None:
    assert structural_union_error(model) is None
    assert reference_marker_error(model) is None


def test_optional_structural_unions_emit_one_of_at_both_positions() -> None:
    field_schema = OptionalHolder.model_json_schema()["properties"]["source"]
    element_schema = OptionalItemsHolder.model_json_schema()["properties"]["sources"]["items"]

    for schema in (field_schema, element_schema):
        structural = next(branch for branch in schema["anyOf"] if "oneOf" in branch)
        assert len(structural["oneOf"]) == 2


def test_optional_structural_unions_render_both_arms_at_both_positions() -> None:
    field_doc = next(iter(iter_field_docs(OptionalHolder)))
    item_doc = next(iter(iter_field_docs(OptionalItemsHolder)))

    assert tuple(arm.doc.model for arm in field_doc.union_arms) == (PlainArm, SecretArm)
    assert tuple(arm.doc.model for arm in item_doc.item_union_arms) == (PlainArm, SecretArm)


@pytest.mark.parametrize(
    ("model", "blob", "canonical"),
    [
        (
            OptionalDefaultHolder,
            {"source": {}},
            {"source": {"secret": "source-demo"}},
        ),
        (
            OptionalDefaultItemsHolder,
            {"sources": [{}, None]},
            {"sources": [{"secret": "source-demo"}, None]},
        ),
    ],
    ids=["optional-field", "optional-collection-element"],
)
def test_optional_structural_unions_fill_the_selected_arm(
    model: type[AgwModel], blob: object, canonical: object
) -> None:
    filled = filled_defaults(model, blob, OWNER)

    assert filled == canonical
    assert model.model_validate(filled) is not None


@pytest.mark.parametrize(
    ("model", "blob"),
    [
        (OptionalHolder, {"source": {"secret": "api-token"}}),
        (OptionalItemsHolder, {"sources": [None, {"secret": "api-token"}]}),
    ],
    ids=["optional-field", "optional-collection-element"],
)
def test_optional_structural_unions_extract_the_selected_arm(model: type[AgwModel], blob: object) -> None:
    assert [ref.name for ref in extract_references(model, blob)] == ["api-token"]


def test_schema_and_field_docs_expose_the_same_alternatives() -> None:
    emitted = Holder.model_json_schema()
    source_schema = emitted["properties"]["sources"]["additionalProperties"]
    assert "anyOf" not in source_schema
    assert len(source_schema["oneOf"]) == 2

    docs = {doc.path: doc for doc in iter_field_docs(Holder)}
    element = docs[("sources",)]
    documented_models = [arm.doc.model for arm in element.item_union_arms]
    assert len(documented_models) == 2
    assert documented_models[0] is PlainArm
    assert documented_models[1] is SecretArm
    assert ("sources", MAPPING_KEY, "value") not in docs


def test_overlapping_table_languages_are_loud_even_without_markers() -> None:
    class First(AgwModel):
        value: str

    class Second(AgwModel):
        value: str
        note: str | None = None

    class Overlapping(AgwModel):
        source: Annotated[First | Second, StructuralUnion()]

    reason = structural_union_error(Overlapping)
    assert reason is not None
    assert "overlapping arms First and Second" in reason


def test_overlapping_table_languages_do_not_select_an_arm() -> None:
    class First(AgwModel):
        value: str

    class Second(AgwModel):
        value: str
        note: str | None = None

    blob = {"value": "text"}

    assert structural_arm_for((First, Second), blob) is None


def test_overlapping_table_languages_do_not_invent_an_extracted_edge() -> None:
    class First(AgwModel):
        value: Annotated[str, SecretRef(usage="the first value")]

    class Second(AgwModel):
        value: str
        note: str | None = None

    class Overlapping(AgwModel):
        source: Annotated[First | Second, StructuralUnion()]

    blob = {"source": {"value": "api-token"}}

    assert list(extract_references(Overlapping, blob)) == []


def test_optional_overlapping_table_languages_stay_loud_without_markers() -> None:
    class First(AgwModel):
        value: str

    class Second(AgwModel):
        value: str
        note: str | None = None

    OverlappingSource = Annotated[First | Second, StructuralUnion()]

    class OptionalOverlap(AgwModel):
        source: OverlappingSource | None = None

    class OptionalOverlapItems(AgwModel):
        sources: list[OverlappingSource | None] = Field(default_factory=list)

    for model in (OptionalOverlap, OptionalOverlapItems):
        reason = structural_union_error(model)
        assert reason is not None
        assert "overlapping arms First and Second" in reason


@pytest.mark.parametrize("other", [str, BaseModel], ids=["non-model", "open-model"])
def test_malformed_structural_arms_are_loud_without_markers(other: object) -> None:
    class Closed(AgwModel):
        value: str

    class Malformed(AgwModel):
        source: Annotated[Closed | other, StructuralUnion()]  # type: ignore[valid-type]

    assert structural_union_error(Malformed) is not None


def test_an_unaddressable_marker_is_still_refused() -> None:
    class First(AgwModel):
        value: Annotated[str, SecretRef(usage="the first value")]

    class Second(AgwModel):
        value: str

    class Overlapping(AgwModel):
        source: Annotated[First | Second, StructuralUnion()]

    assert reference_marker_error(Overlapping) is not None


def test_a_marker_free_validation_alias_is_still_loud() -> None:
    class Aliased(AgwModel):
        value: str = Field(  # type: ignore[pydantic-alias]
            validation_alias=AliasChoices("value", "text")
        )

    class WithAlias(AgwModel):
        source: Annotated[Aliased | SecretArm, StructuralUnion()]

    reason = structural_union_error(WithAlias)
    assert reason is not None
    assert "Aliased.value declares validation alias" in reason


class TaggedValueArm(AgwModel):
    mode: Literal["value"]
    value: str


class TaggedNamedArm(AgwModel):
    mode: Literal["named"]
    named: str


def _assert_selector_is_refused(model: type[AgwModel]) -> None:
    reason = structural_union_error(model)
    assert reason is not None
    assert "combines StructuralUnion with discriminator 'mode'" in reason
    assert "selector-free" in reason


def test_a_structural_union_cannot_also_carry_annotated_discriminator_metadata() -> None:
    class Selected(AgwModel):
        source: Annotated[
            TaggedValueArm | TaggedNamedArm,
            StructuralUnion(),
            Discriminator("mode"),
        ]

    _assert_selector_is_refused(Selected)


def test_a_structural_union_cannot_also_carry_a_field_discriminator() -> None:
    class Selected(AgwModel):
        source: Annotated[TaggedValueArm | TaggedNamedArm, StructuralUnion()] = Field(discriminator="mode")

    _assert_selector_is_refused(Selected)


def test_a_collection_element_structural_union_cannot_carry_a_field_discriminator() -> None:
    class Selected(AgwModel):
        sources: list[
            Annotated[
                TaggedValueArm | TaggedNamedArm,
                StructuralUnion(),
                Field(discriminator="mode"),
            ]
        ]

    _assert_selector_is_refused(Selected)
