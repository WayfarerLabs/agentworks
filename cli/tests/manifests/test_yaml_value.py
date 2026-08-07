"""One rendering of a value, for every surface that shows one.

The property under test is not "this string comes out"; it is that what
comes out is YAML an operator could paste, for any value a model can
declare. So each case asserts the text AND reads it back with the loader
the manifests actually use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path

import pytest
import yaml

from agentworks.manifests.yaml_value import render_value


class Layout(StrEnum):
    TILED = "tiled"


class Weight(Enum):
    """An enum whose values are not strings, so ``str()`` would be wrong
    in a second way."""

    LIGHT = 1


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        ("auto", "auto"),
        (True, "true"),
        (False, "false"),
        (None, "null"),
        (0, "0"),
        (3.5, "3.5"),
        (["zsh", "ripgrep"], "[zsh, ripgrep]"),
        ({"K": "v"}, "{K: v}"),
        (Layout.TILED, "tiled"),
        (Weight.LIGHT, "1"),
        ({"layout": Layout.TILED}, "{layout: tiled}"),
        ([Layout.TILED], "[tiled]"),
        (frozenset({"b", "a"}), "[a, b]"),
        (datetime(2026, 1, 1, tzinfo=UTC), "2026-01-01 00:00:00+00:00"),
    ],
)
def test_a_value_renders_as_the_yaml_a_document_carries(value: object, rendered: str) -> None:
    assert render_value(value) == rendered


@pytest.mark.parametrize(
    "value",
    ["auto", True, False, None, 0, 3.5, ["zsh"], {"K": "v"}, Layout.TILED, Weight.LIGHT, frozenset({"a"})],
)
def test_what_is_rendered_loads_back_as_yaml(value: object) -> None:
    """The point of rendering YAML rather than ``repr``: the reader is
    about to paste this into a document."""
    assert yaml.safe_load(render_value(value)) is not None or value is None


def test_a_value_pyyaml_cannot_represent_renders_rather_than_raising() -> None:
    """These surfaces exist to teach. A plugin whose config declares an
    exotic default should get an imperfect line in its sample, not a
    traceback in place of the whole document."""
    rendered = render_value(Path("/etc/agentworks"))

    assert rendered == "/etc/agentworks"
    assert yaml.safe_load(rendered) == "/etc/agentworks"


def test_a_set_renders_in_a_stable_order() -> None:
    """A generated sample has to be identical across runs or the tests
    that pin it are worthless, and a set's iteration order is not."""
    assert {render_value({"c", "a", "b"}) for _ in range(8)} == {"[a, b, c]"}
