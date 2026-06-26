"""Tests for ``Origin`` -- variant invariants and immutability across all
three variants. The ``code-declared`` factory is exercised here even though
its first real producer (the catalog publisher) doesn't land until Phase 2b;
the type is defined in Phase 1a, so its invariants are pinned here.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agentworks.resources import Origin


def test_operator_declared_factory_populates_file_and_line() -> None:
    o = Origin.operator_declared(file=Path("/x/config.toml"), line=42)
    assert o.variant == "operator-declared"
    assert o.file == Path("/x/config.toml")
    assert o.line == 42
    assert o.source is None


def test_code_declared_factory_populates_source_str() -> None:
    o = Origin.code_declared(source="agentworks.catalog")
    assert o.variant == "code-declared"
    assert o.source == "agentworks.catalog"
    assert o.file is None
    assert o.line is None


def test_auto_declared_factory_populates_source_tuple() -> None:
    o = Origin.auto_declared(source=("vm_template", "azure-prod"))
    assert o.variant == "auto-declared"
    assert o.source == ("vm_template", "azure-prod")
    assert o.file is None
    assert o.line is None


def test_origin_is_immutable() -> None:
    o = Origin.operator_declared(file=Path("/x.toml"), line=1)
    with pytest.raises(FrozenInstanceError):
        o.line = 2  # type: ignore[misc]


def test_origin_equality_per_variant() -> None:
    a = Origin.operator_declared(file=Path("/x.toml"), line=1)
    b = Origin.operator_declared(file=Path("/x.toml"), line=1)
    c = Origin.operator_declared(file=Path("/x.toml"), line=2)
    assert a == b
    assert a != c

    d = Origin.code_declared(source="agentworks.catalog")
    e = Origin.code_declared(source="agentworks.catalog")
    f = Origin.code_declared(source="other")
    assert d == e
    assert d != f

    g = Origin.auto_declared(source=("vm_template", "default"))
    h = Origin.auto_declared(source=("vm_template", "default"))
    i = Origin.auto_declared(source=("vm_template", "other"))
    assert g == h
    assert g != i


def test_variants_do_not_cross_compare_equal() -> None:
    # An operator-declared and auto-declared with same-looking incidental
    # data still differ by variant.
    op = Origin.operator_declared(file=Path("/x"), line=1)
    code = Origin.code_declared(source="agentworks.catalog")
    auto = Origin.auto_declared(source=("vm_template", "default"))
    assert op != code
    assert op != auto
    assert code != auto
