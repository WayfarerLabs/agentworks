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
    ("value", "rendered", "loaded"),
    [
        ("auto", "auto", "auto"),
        (True, "true", True),
        (False, "false", False),
        (None, "null", None),
        (0, "0", 0),
        (3.5, "3.5", 3.5),
        (["zsh", "ripgrep"], "[zsh, ripgrep]", ["zsh", "ripgrep"]),
        ({"K": "v"}, "{K: v}", {"K": "v"}),
        (Layout.TILED, "tiled", "tiled"),
        (Weight.LIGHT, "1", 1),
        ({"layout": Layout.TILED}, "{layout: tiled}", {"layout": "tiled"}),
        ([Layout.TILED], "[tiled]", ["tiled"]),
        (frozenset({"b", "a"}), "[a, b]", ["a", "b"]),
        (datetime(2026, 1, 1, tzinfo=UTC), "2026-01-01 00:00:00+00:00", datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_a_value_renders_as_the_yaml_a_document_carries(value: object, rendered: str, loaded: object) -> None:
    """The text, and what the loader makes of it, over one case list.

    Both halves per case, because the point of rendering YAML rather than
    ``repr`` is that the reader is about to paste this into a document:
    text nobody reads back proves the renderer is self-consistent and not
    that it is right. The round trip used to be a second parametrize
    asserting only ``is not None``, which no mutation of this module could
    fail (``repr`` in place of the dumper left ten of its eleven cases
    green); an exact loaded value is what makes it a check.
    """
    assert render_value(value) == rendered
    assert yaml.safe_load(rendered) == loaded


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
