"""``replace_fields``: the shared row-stamping helper.

It exists because a declared row is a MODEL while the capability marker
rows beside it are still frozen dataclasses, and the framework code that
stamps both (origin, the auto-declared description) must not branch on
which shape it got.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentworks.declared_resource import replace_fields
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
    input, and a re-validation here would reject a ``None`` the framework
    deliberately writes into a field the operator must fill."""
    assert replace_fields(_ModelRow(name="a"), name=None).name is None


def test_something_that_is_neither_is_a_loud_failure() -> None:
    """Never a silent pass-through: the caller gets an object back either
    way, so a no-op on an unrecognized shape leaves the row unstamped and
    nothing says so until something downstream reads the missing field."""
    with pytest.raises(StateError, match="neither a frozen dataclass nor a model"):
        replace_fields(object(), origin="built-in")
