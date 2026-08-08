"""One declared shorthand, three derivations that cannot disagree.

``ScalarShorthand`` exists because the fact "this table may also be
written as a bare string" had been authored twice on ``EnvEntry`` (a
before-validator and a hand-rolled ``__get_pydantic_json_schema__``) and
the third consumer, the field-documentation stream, had no way to learn
it at all. So these tests ask the same question of each derivation and
compare the answers, rather than pinning three copies of an expectation.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

import pytest
from pydantic import Discriminator, Field

from agentworks.errors import StateError
from agentworks.schema import (
    MAPPING_KEY,
    AgwModel,
    RefOwner,
    ScalarShorthand,
    SecretRef,
    UnionScalarShorthand,
    extract_references,
    filled_defaults,
    iter_field_docs,
    render_type,
    union_scalar_shorthand_error,
)

from ._fixture_models import ShorthandHolder, ShorthandLike, ShorthandTemplatedLike

OWNER = RefOwner(kind="vm-template", name="dev")


def test_the_shorthand_spelling_validates_to_the_long_one() -> None:
    assert ShorthandLike.model_validate("a value") == ShorthandLike(value="a value")


def test_the_long_spelling_still_validates() -> None:
    assert ShorthandLike.model_validate({"secret": "npm-token"}).secret == "npm-token"


def test_a_value_that_is_neither_spelling_is_still_refused() -> None:
    """The fold widens what is accepted by exactly one shape. An integer
    is not it, and the closed-world posture has to survive the widening."""
    with pytest.raises(ValueError, match="valid dictionary"):
        ShorthandLike.model_validate(7)


def test_a_boolean_is_not_an_integer_shorthand() -> None:
    """``bool`` is a subclass of ``int``, so a fold asked with
    ``isinstance`` would take ``true`` for an integer shorthand: the
    silent coercion the strict posture exists to refuse."""

    class Counted(AgwModel):
        scalar_shorthand: ClassVar = ScalarShorthand(annotation=int, field="count")

        count: int | None = None

    assert Counted.model_validate(7) == Counted(count=7)
    with pytest.raises(ValueError, match="valid dictionary"):
        Counted.model_validate(True)


def test_the_shorthand_is_folded_before_owner_templated_defaults_are_filled() -> None:
    """The order the two rewrites happen in, asserted through what it
    decides rather than through which function runs first.

    The boundary fill acts on a mapping, so it folds a bare-scalar value
    itself before filling: folded after, an owner-templated field would
    resolve for the operator who wrote the table form and silently not
    for the one who wrote the scalar, which is the same value spelled two
    ways.
    """
    short = ShorthandTemplatedLike.model_validate(filled_defaults(ShorthandTemplatedLike, "a value", OWNER))
    long = ShorthandTemplatedLike.model_validate(filled_defaults(ShorthandTemplatedLike, {"value": "a value"}, OWNER))

    assert short.token == "shorthand-dev"
    assert short == long


def test_the_emitted_schema_offers_the_shorthand_arm() -> None:
    """The arm an editor needs, without which every plaintext env value an
    operator writes is red-underlined."""
    emitted = ShorthandLike.model_json_schema()

    assert emitted["anyOf"][0] == {"type": "string"}
    assert emitted["anyOf"][1]["type"] == "object"


def test_the_marker_corrections_still_apply_inside_the_shorthand_arm() -> None:
    """The two derivations compose: the shorthand widens the model and the
    reference marker still lands on the property, one level in."""
    table = ShorthandLike.model_json_schema()["anyOf"][1]

    assert "x-agw-ref" in table["properties"]["secret"]


def test_the_stream_names_both_spellings_wherever_the_model_is_held() -> None:
    """One declaration reaches a field, a table, and a list alike. The
    shipped case is the table (five spec models hold an ``EnvEntry``
    one), and nothing about the declaration is per-field."""
    rendered = {doc.path: render_type(doc.annotation) for doc in iter_field_docs(ShorthandHolder)}

    assert rendered[("entry",)] == "string or table or null"
    assert rendered[("entries",)] == "table of string or table"
    assert rendered[("entry_list",)] == "list of string or table"


def test_the_stream_still_expands_the_table_form() -> None:
    """A shorthand ADDS a spelling; it does not remove the block. An
    operator who needs the secret form still has to be told what is in
    it."""
    paths = [doc.path for doc in iter_field_docs(ShorthandHolder)]

    assert ("entries", MAPPING_KEY, "secret") in paths
    assert ("entry", "value") in paths


def test_a_shorthand_folding_into_a_field_that_does_not_exist_is_refused_at_import() -> None:
    """An author's typo, caught where the author is. It would otherwise
    surface as a closed-world ``extra_forbidden`` on a key no operator
    wrote, at the moment some operator happened to use the short form."""
    with pytest.raises(StateError, match="not one of its fields"):

        class Mistyped(AgwModel):
            scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="valeu")

            value: str | None = None


def test_a_shorthand_spelled_as_something_no_document_carries_is_refused() -> None:
    with pytest.raises(StateError, match="a scalar shorthand may be spelled as"):
        ScalarShorthand(annotation=dict, field="value")


def test_a_marked_field_inside_a_shorthand_model_still_reaches_the_graph() -> None:
    """The fold changes the shape a blob arrives in, not what it means.
    A secret named the long way is the same edge it always was."""
    from agentworks.schema import extract_references

    class Holder(AgwModel):
        entries: dict[str, ShorthandLike] = Field(default_factory=dict)

    found = extract_references(Holder, {"entries": {"A": {"secret": "npm-token"}, "B": "plaintext"}})

    assert [(ref.kind, ref.name) for ref in found] == [("secret", "npm-token")]


def test_the_declaration_is_the_only_place_the_spelling_is_written() -> None:
    """The anti-drift pin, and the reason this class exists: the string
    arm in emitted schema, the value the loader accepts, and the type the
    stream renders all trace to one declaration, so a model that declares
    none offers none anywhere.
    """

    class Plain(AgwModel):
        value: str | None = None
        secret: Annotated[str, SecretRef(usage="a plain secret")] | None = None

    assert Plain.scalar_shorthand is None
    assert "anyOf" not in Plain.model_json_schema()
    with pytest.raises(ValueError, match="valid dictionary"):
        Plain.model_validate("a value")

    class Holder(AgwModel):
        entries: dict[str, Plain] = Field(default_factory=dict)

    rendered = {doc.path: render_type(doc.annotation) for doc in iter_field_docs(Holder)}
    assert rendered[("entries",)] == "table of table"


def test_an_arm_shorthand_alone_does_not_dispatch_a_tagged_union() -> None:
    """Pydantic selects a tag before the arm validator can fold a scalar.

    The walkers and human surface must not infer a choice validation does
    not make, and conformance must reject the plugin model before seating.
    """

    class Stored(AgwModel):
        scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="secret")

        mode: Literal["stored"]
        secret: Annotated[str, SecretRef(usage="a token")]

    class Holder(AgwModel):
        token: Annotated[Stored, Discriminator("mode")]

    with pytest.raises(ValueError, match="valid dictionary"):
        Holder.model_validate({"token": "named"})
    assert filled_defaults(Holder, {"token": "named"}, OWNER) == {"token": "named"}
    assert extract_references(Holder, {"token": "named"}) == ()
    (token,) = [doc for doc in iter_field_docs(Holder) if doc.path == ("token",)]
    assert render_type(token.annotation) == "table"
    assert "declares no UnionScalarShorthand" in (union_scalar_shorthand_error(Holder) or "")


def test_one_union_scalar_declaration_aligns_every_derivation() -> None:
    """The union declaration selects an arm while deriving its scalar
    type and fold field from that arm's model-level shorthand."""

    class Stored(AgwModel):
        scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="secret")

        mode: Literal["stored"]
        secret: Annotated[str, SecretRef(usage="a token")]

    class Holder(AgwModel):
        token: Annotated[
            Stored,
            UnionScalarShorthand(discriminator="mode", arm=Stored),
        ]

    assert Holder.model_validate({"token": "named"}).token == Stored(mode="stored", secret="named")
    assert filled_defaults(Holder, {"token": "named"}, OWNER) == {"token": "named"}
    assert [(ref.kind, ref.name) for ref in extract_references(Holder, {"token": "named"})] == [("secret", "named")]
    (token,) = [doc for doc in iter_field_docs(Holder) if doc.path == ("token",)]
    assert render_type(token.annotation) == "string or table"
    assert Holder.model_json_schema()["properties"]["token"]["anyOf"][0] == {"type": "string"}
    assert union_scalar_shorthand_error(Holder) is None


def test_a_collection_union_documents_only_its_selected_scalar_arm() -> None:
    """Element recursion must not widen every tagged arm independently.

    Both arms accept a scalar when validated alone, but the union-level
    declaration selects only the string arm. Validation, emitted schema,
    and field documentation must expose exactly that scalar type while
    retaining both tagged table arms.
    """

    class StringArm(AgwModel):
        scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="value")

        mode: Literal["string"]
        value: str

    class IntegerArm(AgwModel):
        scalar_shorthand: ClassVar = ScalarShorthand(annotation=int, field="value")

        mode: Literal["integer"]
        value: int

    Element = Annotated[
        StringArm | IntegerArm,
        UnionScalarShorthand(discriminator="mode", arm=StringArm),
    ]

    class Holder(AgwModel):
        values: list[Element] = Field(default_factory=list)

    assert Holder.model_validate({"values": ["short"]}).values == [StringArm(mode="string", value="short")]
    assert Holder.model_validate({"values": [{"mode": "integer", "value": 7}]}).values == [
        IntegerArm(mode="integer", value=7)
    ]
    with pytest.raises(ValueError, match="valid dictionary"):
        Holder.model_validate({"values": [7]})

    schema_items = Holder.model_json_schema()["properties"]["values"]["items"]
    assert [arm.get("type") for arm in schema_items["anyOf"] if "type" in arm] == ["string"]

    (values,) = [doc for doc in iter_field_docs(Holder) if doc.path == ("values",)]
    assert render_type(values.annotation) == "list of string or table"
    assert union_scalar_shorthand_error(Holder) is None


def test_a_collapsed_collection_union_keeps_scalar_dispatch_explicit() -> None:
    """A one-arm union is a model at runtime but remains tagged surface.

    With the union declaration, every derivation selects and folds the
    stored arm. With only the arm's standalone shorthand, tag dispatch
    rejects the scalar and no framework surface may infer otherwise.
    """

    class Stored(AgwModel):
        scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="secret")

        mode: Literal["stored"]
        secret: Annotated[
            str,
            SecretRef(usage="a collection token", default_template="collection-token-{owner_name}"),
        ]

    ExplicitElement = Annotated[
        Stored,
        UnionScalarShorthand(discriminator="mode", arm=Stored),
    ]

    class ExplicitHolder(AgwModel):
        tokens: list[ExplicitElement] = Field(default_factory=list)

    assert ExplicitHolder.model_validate({"tokens": ["named"]}).tokens == [Stored(mode="stored", secret="named")]
    assert filled_defaults(ExplicitHolder, {"tokens": ["named"]}, OWNER) == {"tokens": ["named"]}
    assert [(ref.kind, ref.name) for ref in extract_references(ExplicitHolder, {"tokens": ["named"]})] == [
        ("secret", "named")
    ]

    defaulted = filled_defaults(ExplicitHolder, {"tokens": [{"mode": "stored"}]}, OWNER)
    assert defaulted == {"tokens": [{"mode": "stored", "secret": "collection-token-dev"}]}
    assert [(ref.kind, ref.name) for ref in extract_references(ExplicitHolder, defaulted)] == [
        ("secret", "collection-token-dev")
    ]

    explicit_schema = ExplicitHolder.model_json_schema()["properties"]["tokens"]["items"]
    assert explicit_schema["anyOf"][0] == {"type": "string"}
    (explicit_doc,) = [doc for doc in iter_field_docs(ExplicitHolder) if doc.path == ("tokens",)]
    assert render_type(explicit_doc.annotation) == "list of string or table"
    assert [arm.tag for arm in explicit_doc.item_union_arms] == ["stored"]
    assert union_scalar_shorthand_error(ExplicitHolder) is None

    class TaggedOnlyHolder(AgwModel):
        tokens: list[Annotated[Stored, Discriminator("mode")]] = Field(default_factory=list)

    with pytest.raises(ValueError, match="valid dictionary"):
        TaggedOnlyHolder.model_validate({"tokens": ["named"]})
    assert filled_defaults(TaggedOnlyHolder, {"tokens": ["named"]}, OWNER) == {"tokens": ["named"]}
    assert extract_references(TaggedOnlyHolder, {"tokens": ["named"]}) == ()

    tagged_schema = TaggedOnlyHolder.model_json_schema()["properties"]["tokens"]["items"]
    assert "anyOf" not in tagged_schema
    (tagged_doc,) = [doc for doc in iter_field_docs(TaggedOnlyHolder) if doc.path == ("tokens",)]
    assert render_type(tagged_doc.annotation) == "list of table"
    assert [arm.tag for arm in tagged_doc.item_union_arms] == ["stored"]
    assert "declares no UnionScalarShorthand" in (union_scalar_shorthand_error(TaggedOnlyHolder) or "")


def test_union_scalar_conformance_refuses_ambiguous_and_mismatched_declarations() -> None:
    class Stored(AgwModel):
        scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="secret")

        mode: Literal["stored"]
        secret: str

    class Other(AgwModel):
        scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="secret")

        mode: Literal["other"]
        secret: str

    class Ambiguous(AgwModel):
        token: Annotated[
            Stored,
            UnionScalarShorthand(discriminator="mode", arm=Stored),
            UnionScalarShorthand(discriminator="mode", arm=Stored),
        ]

    class Mismatched(AgwModel):
        token: Annotated[
            Stored,
            UnionScalarShorthand(discriminator="mode", arm=Other),
        ]

    assert "declares 2 union scalar shorthands" in (union_scalar_shorthand_error(Ambiguous) or "")
    assert "occurs 0 times" in (union_scalar_shorthand_error(Mismatched) or "")
