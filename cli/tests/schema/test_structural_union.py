"""The model-derived contract for untagged structural unions."""

from __future__ import annotations

from typing import Annotated, ClassVar

import pytest
from pydantic import AliasChoices, BaseModel, Field

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

OWNER = RefOwner(kind="fixture", name="demo")


class PlainArm(AgwModel):
    """A plaintext source."""

    scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="value")

    value: str


class SecretArm(AgwModel):
    """A named secret source."""

    secret: Annotated[str, SecretRef(usage="the fixture source")]


Source = Annotated[PlainArm | SecretArm, StructuralUnion()]


class Holder(AgwModel):
    sources: dict[str, Source] = Field(default_factory=dict)


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


def test_owner_default_filling_uses_the_same_structural_selection() -> None:
    class DefaultSecretArm(AgwModel):
        secret: Annotated[
            str,
            SecretRef(usage="the defaulted source", default_template="source-{owner_name}"),
        ]

    class DefaultHolder(AgwModel):
        source: Annotated[PlainArm | DefaultSecretArm, StructuralUnion()]

    filled = filled_defaults(DefaultHolder, {"source": {}}, OWNER)

    assert filled == {"source": {"secret": "source-demo"}}
    assert DefaultHolder.model_validate(filled).source == DefaultSecretArm(secret="source-demo")
    assert [ref.name for ref in extract_references(DefaultHolder, filled)] == ["source-demo"]


def test_schema_and_field_docs_expose_the_same_alternatives() -> None:
    emitted = Holder.model_json_schema()
    source_schema = emitted["properties"]["sources"]["additionalProperties"]
    assert "anyOf" not in source_schema
    assert len(source_schema["oneOf"]) == 2

    docs = {doc.path: doc for doc in iter_field_docs(Holder)}
    element = docs[("sources",)]
    assert [arm.doc.model for arm in element.item_union_arms] == [PlainArm, SecretArm]
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
        value: str = Field(validation_alias=AliasChoices("value", "text"))

    class WithAlias(AgwModel):
        source: Annotated[Aliased | SecretArm, StructuralUnion()]

    reason = structural_union_error(WithAlias)
    assert reason is not None
    assert "Aliased.value declares validation alias" in reason
