"""Tests for the shared declared-row helpers.

``replace_fields`` and ``ResourceName`` both exist because a declared row
is a MODEL while the capability marker rows beside it are still frozen
dataclasses; each helper is what keeps the framework code that touches
both from branching on which shape it got.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError as PydanticValidationError

from agentworks.declared_resource import ResourceName, replace_fields
from agentworks.errors import StateError
from agentworks.schema import AgwModel


@dataclass(frozen=True, kw_only=True)
class _Row:
    name: str
    origin: str | None = None


class _ModelRow(AgwModel):
    """A frozen model row, the shape a declared resource takes."""

    name: str
    origin: str | None = None


class _Named(AgwModel):
    """A row whose name is validated at load, with its kind's cap."""

    name: ResourceName(max_length=8)  # type: ignore[valid-type]


# -- replace_fields ------------------------------------------------------------


def test_a_frozen_dataclass_row_is_replaced() -> None:
    assert replace_fields(_Row(name="a"), origin="built-in").origin == "built-in"


def test_a_frozen_model_row_is_replaced() -> None:
    assert replace_fields(_ModelRow(name="a"), origin="built-in").origin == "built-in"


def test_the_original_row_is_untouched() -> None:
    row = _ModelRow(name="a")

    replace_fields(row, origin="built-in")

    assert row.origin is None


def test_the_model_path_does_not_re_validate() -> None:
    """Framework-supplied values only, exactly as ``dataclasses.replace``
    behaves: the caller is stamping provenance, not accepting operator
    input, and a re-validation here would reject the ``None`` the
    migrator's normalization writes into a required field."""
    assert replace_fields(_ModelRow(name="a"), name=None).name is None


def test_something_that_is_neither_is_a_loud_failure() -> None:
    """Never a silent pass-through: a no-op on an unrecognized shape is
    how the migrator's equivalence check would start comparing the very
    fields this helper exists to strip."""
    with pytest.raises(StateError, match="neither a frozen dataclass nor a model"):
        replace_fields(object(), origin="built-in")


# -- ResourceName --------------------------------------------------------------


def test_a_conforming_name_validates() -> None:
    assert _Named.model_validate({"name": "lab-one"}).name == "lab-one"


def test_a_non_conforming_name_reports_the_naming_rule() -> None:
    with pytest.raises(PydanticValidationError) as caught:
        _Named.model_validate({"name": "Lab"})

    assert "invalid name 'Lab'" in str(caught.value)


def test_an_over_length_name_reports_the_callers_cap() -> None:
    with pytest.raises(PydanticValidationError) as caught:
        _Named.model_validate({"name": "a" * 9})

    assert "max 8" in str(caught.value)


def test_the_naming_error_reaches_pydantic_rather_than_escaping_it() -> None:
    """``validate_name`` raises the agentworks ``ValidationError``, which
    is NOT a ``ValueError``, so pydantic would let it escape
    ``model_validate`` and bypass the error bridge. The wrapper converts
    it, and this is the assertion that the conversion happened."""
    with pytest.raises(PydanticValidationError) as caught:
        _Named.model_validate({"name": "Lab"})

    (detail,) = caught.value.errors(include_url=False)
    assert detail["type"] == "value_error"
