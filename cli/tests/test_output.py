"""Unit tests for pure helpers in ``agentworks.output``."""

from __future__ import annotations

from agentworks import output


def test_count_pluralizes_regular_nouns() -> None:
    assert output.count(0, "package") == "0 packages"
    assert output.count(1, "package") == "1 package"
    assert output.count(2, "package") == "2 packages"


def test_count_multiword_noun() -> None:
    assert output.count(1, "apt package") == "1 apt package"
    assert output.count(3, "apt package") == "3 apt packages"


def test_count_irregular_plural() -> None:
    assert output.count(1, "PATH entry", "PATH entries") == "1 PATH entry"
    assert output.count(4, "PATH entry", "PATH entries") == "4 PATH entries"
