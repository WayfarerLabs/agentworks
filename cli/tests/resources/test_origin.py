"""Tests for ``Origin`` -- variant invariants and immutability across all
three variants.
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


def test_built_in_factory_populates_source_str() -> None:
    o = Origin.built_in(source="agentworks.manifests.builtin/apt-sources.yaml")
    assert o.variant == "built-in"
    assert o.source == "agentworks.manifests.builtin/apt-sources.yaml"
    assert o.file is None
    assert o.line is None


def test_auto_declared_factory_populates_source_tuple() -> None:
    o = Origin.auto_declared(source=("vm-template", "azure-prod"))
    assert o.variant == "auto-declared"
    assert o.source == ("vm-template", "azure-prod")
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

    d = Origin.built_in(source="agentworks.manifests.builtin/apt-sources.yaml")
    e = Origin.built_in(source="agentworks.manifests.builtin/apt-sources.yaml")
    f = Origin.built_in(source="other")
    assert d == e
    assert d != f

    g = Origin.auto_declared(source=("vm-template", "default"))
    h = Origin.auto_declared(source=("vm-template", "default"))
    i = Origin.auto_declared(source=("vm-template", "other"))
    assert g == h
    assert g != i


def test_variants_do_not_cross_compare_equal() -> None:
    # An operator-declared and auto-declared with same-looking incidental
    # data still differ by variant.
    op = Origin.operator_declared(file=Path("/x"), line=1)
    code = Origin.built_in(source="agentworks.manifests.builtin/apt-sources.yaml")
    auto = Origin.auto_declared(source=("vm-template", "default"))
    assert op != code
    assert op != auto
    assert code != auto
