"""Interpolation policy agrees across schema, field facts and consuming adapters."""

from __future__ import annotations

from operator import or_
from types import GenericAlias
from typing import Annotated, ClassVar

import pytest
from pydantic import Field, create_model
from typing_extensions import TypeAliasType as BackportTypeAliasType  # noqa: UP035

from agentworks.env.entry import EnvEntry, PlaintextEnvEntry
from agentworks.errors import StateError, ValidationError
from agentworks.interpolation_fields import render_list_field, render_scalar_field
from agentworks.schema import (
    AgwModel,
    Interpolated,
    ScalarShorthand,
    SecretRef,
    iter_field_docs,
)


class Consumer(AgwModel):
    value: Annotated[str, Interpolated()]
    optional: Annotated[str, Interpolated()] | None = None
    wrapped_optional: Annotated[str | None, Interpolated()] = None
    args: list[Annotated[str, Interpolated()]] = Field(default_factory=list)
    literal: str = "${agw:unterminated"
    literal_args: list[str] = Field(default_factory=list)


def test_declaration_projects_into_schema_field_facts_and_runtime() -> None:
    source = Consumer(
        value="${agw:user_name}",
        args=["$${agw:user_name}", "${agw:workspace_dir}"],
        literal_args=["${agw:unterminated"],
    )
    values = {"user_name": "operator", "workspace_dir": "${agw:user_name}"}
    properties = Consumer.model_json_schema()["properties"]
    docs = {field.path[0]: field.interpolation for field in iter_field_docs(Consumer)}

    assert properties["value"]["x-agw-interpolation"] is True
    assert properties["args"]["items"]["x-agw-interpolation"] is True
    assert docs == {
        "value": "scalar",
        "optional": "scalar",
        "wrapped_optional": "scalar",
        "args": "items",
        "literal": None,
        "literal_args": None,
    }
    for name in ("optional", "wrapped_optional"):
        string_arm, null_arm = properties[name]["anyOf"]
        assert string_arm["x-agw-interpolation"] is True
        assert "x-agw-interpolation" not in null_arm
        assert "x-agw-interpolation" not in properties[name]
    assert "x-agw-interpolation" not in properties["literal"]
    assert render_scalar_field(Consumer, "value", source.value, values, location="owner.value") == "operator"
    assert render_scalar_field(Consumer, "optional", source.optional, {}, location="owner.optional") is None
    assert source.literal == "${agw:unterminated"
    assert render_list_field(Consumer, "args", source.args, values, location="owner.args") == [
        "${agw:user_name}",
        "${agw:user_name}",
    ]
    assert source.literal_args == ["${agw:unterminated"]
    assert source.value == "${agw:user_name}"
    assert source.args == ["$${agw:user_name}", "${agw:workspace_dir}"]


def test_explicit_rendering_requires_the_matching_annotation() -> None:
    source = Consumer(value="unchanged", literal_args=["${agw:unterminated"])
    for name, value in (("literal", source.literal), ("literal", None), ("args", "unchanged")):
        with pytest.raises(StateError) as caught:
            render_scalar_field(Consumer, name, value, {}, location=f"owner.{name}")
        assert caught.value.entity_name == f"owner.{name}"
    for name in ("literal_args", "value"):
        with pytest.raises(StateError) as caught:
            render_list_field(Consumer, name, [], {}, location=f"owner.{name}")
        assert caught.value.entity_name == f"owner.{name}"


def test_list_failure_identifies_the_consumed_field_and_element() -> None:
    with pytest.raises(ValidationError) as caught:
        render_list_field(Consumer, "args", ["unchanged", "${agw:session_name}"], {}, location="owner.args")
    assert caught.value.entity_name == "owner.args[1]"


def test_inserted_encoding_keeps_authored_source_and_one_pass_data_distinct() -> None:
    encoded: list[str] = []

    def encode(value: str) -> str:
        encoded.append(value)
        return f"<{value}>"

    result = render_scalar_field(
        Consumer,
        "value",
        "$HOME/$${agw:user_name}/${agw:workspace_dir}",
        {"workspace_dir": "${agw:user_name}"},
        location="profile.path",
        encode_inserted=encode,
    )
    assert result == "$HOME/<${agw:>user_name}/<${agw:user_name}>"
    assert encoded == ["${agw:", "${agw:user_name}"]


def test_scalar_shorthand_derives_interpolation_from_its_target_field() -> None:
    schema = PlaintextEnvEntry.model_json_schema()
    scalar_arm, object_arm = schema["anyOf"]
    assert scalar_arm["x-agw-interpolation"] == object_arm["properties"]["value"]["x-agw-interpolation"]
    assert next(iter_field_docs(PlaintextEnvEntry)).interpolation == "scalar"
    entry_schema = EnvEntry.model_json_schema()
    assert entry_schema["$defs"]["PlaintextEnvEntry"]["anyOf"][0]["x-agw-interpolation"] is True
    assert "x-agw-interpolation" not in entry_schema["$defs"]["SecretEnvEntry"]["properties"]["secret"]
    assert EnvEntry.model_validate("${agw:unknown}").value == "${agw:unknown}"


@pytest.mark.parametrize(
    "annotation",
    [
        Annotated[int, Interpolated()],
        Annotated[list[str], Interpolated()],
        dict[Annotated[str, Interpolated()], str],
        list[list[Annotated[str, Interpolated()]]],
        list[Annotated[str, Interpolated()] | None],
        Annotated[str, Interpolated(), SecretRef(usage="a credential")],
        Annotated[str, Interpolated()] | int,
    ],
)
def test_model_boundary_rejects_unsupported_marker_placement(annotation: object) -> None:
    with pytest.raises(StateError):
        create_model("Unsupported", __base__=AgwModel, value=(annotation, ...))


def test_nested_shorthand_models_keep_one_interpolation_declaration() -> None:
    class Text(AgwModel):
        scalar_shorthand: ClassVar = ScalarShorthand(annotation=str, field="value")
        value: Annotated[str, Interpolated()]

    class Holder(AgwModel):
        text: Text

    source = Holder.model_validate({"text": "${agw:user_name}"})
    assert source.text.value == "${agw:user_name}"
    assert {field.path: field.interpolation for field in iter_field_docs(Holder)} == {
        ("text",): None,
        ("text", "value"): "scalar",
    }
    scalar, table = Holder.model_json_schema()["$defs"]["Text"]["anyOf"]
    assert scalar["x-agw-interpolation"] is table["properties"]["value"]["x-agw-interpolation"] is True


type RuntimeText = Annotated[str, Interpolated()]
type RecursiveText = list[RecursiveText] | str
type RecursiveRuntimeText = list[RecursiveRuntimeText] | Annotated[str, Interpolated()]


def test_aliases_cannot_hide_interpolation_but_plain_recursive_aliases_still_work() -> None:

    for alias in (
        RuntimeText,
        RecursiveRuntimeText,
        BackportTypeAliasType("BackportRuntimeText", Annotated[str, Interpolated()]),
        BackportTypeAliasType("ForwardRuntimeText", "Annotated[str, Interpolated()]"),
    ):
        for annotation in (alias, or_(alias, None), GenericAlias(list, alias)):
            with pytest.raises(StateError):
                create_model("UnsupportedAlias", __base__=AgwModel, value=(annotation, ...))

    recursive = create_model("RecursiveAlias", __base__=AgwModel, value=(RecursiveText, ...))
    source = recursive.model_validate({"value": ["${agw:user_name}"]})
    assert source.model_dump()["value"] == ["${agw:user_name}"]
    assert next(iter_field_docs(recursive)).interpolation is None
    plain = create_model(
        "PlainAlias",
        __base__=AgwModel,
        value=(BackportTypeAliasType("PlainText", "str"), ...),
        other=(Annotated[str, Interpolated()], "unrelated"),
    )
    assert plain.model_validate({"value": "${agw:user_name}"}).model_dump()["value"] == "${agw:user_name}"
