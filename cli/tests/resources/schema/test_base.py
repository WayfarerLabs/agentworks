"""Tests for ``AgwModel`` / ``AgwRootModel``: the shared posture.

Each test pins one setting of the shared config, because every one of
them is a promise the rest of the framework builds on.
"""

from __future__ import annotations

import pytest
from pydantic import Field, ValidationError

from agentworks.resources.schema import AgwModel, AgwRootModel


class Nested(AgwModel):
    port: int = 22


class Sample(AgwModel):
    name: str
    """The operator-facing name."""

    described: str = Field(default="x", description="explicit wins")
    """The docstring that loses to the explicit description."""

    ratio: float = 1.0
    flag: bool = False
    nested: Nested = Nested()


class BadDefault(AgwModel):
    count: int = "eight"  # type: ignore[assignment]


class StrRoot(AgwRootModel[str]):
    """A root model over a bare string, as a backend mapping is."""


class NestedRoot(AgwRootModel[Nested]):
    """A root model whose root is itself a mapping-shaped model."""


def test_unknown_key_is_a_hard_error() -> None:
    with pytest.raises(ValidationError) as exc:
        Sample.model_validate({"name": "a", "bogus": 1})
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_unknown_key_in_a_nested_model_is_a_hard_error() -> None:
    with pytest.raises(ValidationError) as exc:
        Sample.model_validate({"name": "a", "nested": {"port": 22, "bogus": 1}})
    assert exc.value.errors()[0]["loc"] == ("nested", "bogus")


def test_instances_are_frozen() -> None:
    sample = Sample.model_validate({"name": "a"})
    with pytest.raises(ValidationError) as exc:
        sample.name = "b"
    assert exc.value.errors()[0]["type"] == "frozen_instance"


def test_strict_rejects_a_quoted_integer() -> None:
    class Ported(AgwModel):
        port: int

    with pytest.raises(ValidationError) as exc:
        Ported.model_validate({"port": "8"})
    assert exc.value.errors()[0]["type"] == "int_type"


def test_strict_rejects_an_integer_for_a_boolean() -> None:
    with pytest.raises(ValidationError) as exc:
        Sample.model_validate({"name": "a", "flag": 1})
    assert exc.value.errors()[0]["type"] == "bool_type"


def test_strict_rejects_none_for_a_non_optional_field() -> None:
    with pytest.raises(ValidationError) as exc:
        Sample.model_validate({"name": None})
    assert exc.value.errors()[0]["type"] == "string_type"


def test_strict_accepts_an_integer_for_a_float_field() -> None:
    # The one widening strict mode allows, and the one we want: an
    # operator writing ``memory: 8`` against ``memory: float`` is not
    # making a mistake. Pinned so the question is not re-opened.
    assert Sample.model_validate({"name": "a", "ratio": 8}).ratio == 8.0


def test_a_bad_default_survives_class_definition_and_fails_on_omission() -> None:
    # ``validate_default`` fires when a document OMITS the field, not at
    # class definition: ``BadDefault`` imported fine at module load.
    assert BadDefault.model_validate({"count": 3}).count == 3
    with pytest.raises(ValidationError) as exc:
        BadDefault.model_validate({})
    assert exc.value.errors()[0]["type"] == "int_type"


def test_a_nested_instance_is_revalidated_rather_than_trusted() -> None:
    # ``model_construct`` skips validation, so this is the shape a caller
    # could otherwise smuggle past the boundary by binding an
    # already-built instance.
    smuggled = Nested.model_construct(port="22")  # type: ignore[arg-type]
    with pytest.raises(ValidationError) as exc:
        Sample.model_validate({"name": "a", "nested": smuggled})
    assert exc.value.errors()[0]["type"] == "int_type"


def test_attribute_docstrings_become_descriptions() -> None:
    assert Sample.model_fields["name"].description == "The operator-facing name."


def test_an_explicit_description_wins_over_the_docstring() -> None:
    assert Sample.model_fields["described"].description == "explicit wins"


def test_a_root_model_over_a_scalar_rejects_a_table() -> None:
    assert StrRoot.model_validate("op://vault/item").root == "op://vault/item"
    with pytest.raises(ValidationError):
        StrRoot.model_validate({"account": "a"})


def test_a_root_model_carries_closed_world_through_its_root() -> None:
    with pytest.raises(ValidationError) as exc:
        NestedRoot.model_validate({"port": 22, "bogus": 1})
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_a_root_model_is_frozen() -> None:
    root = StrRoot.model_validate("x")
    with pytest.raises(ValidationError):
        root.root = "y"
