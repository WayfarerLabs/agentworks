"""``TopicProse``: the authored prose layer.

Small on purpose. What is worth pinning is the normalization (prose is
written as an indented literal at the declaration site) and the refusal of
empty prose, since an empty overview would render as a blank section on two
surfaces rather than as nothing.
"""

from __future__ import annotations

import pytest

from agentworks.errors import StateError
from agentworks.topics import TopicProse, prose_of

PROSE = TopicProse(
    title="Fixture things",
    overview="""
    A fixture thing is what a test declares.

    Second paragraph.
    """,
)


def test_overview_is_dedented_so_prose_can_be_written_where_it_belongs() -> None:
    assert PROSE.overview == "A fixture thing is what a test declares.\n\nSecond paragraph."


@pytest.mark.parametrize(("title", "overview"), [("", "text"), ("   ", "text"), ("Title", "   ")])
def test_empty_prose_is_refused_rather_than_rendered_blank(title: str, overview: str) -> None:
    """The contract's answer for "nothing useful to say" is to contribute
    no prose at all, so empty prose is a mistake rather than a choice."""
    with pytest.raises(StateError, match="non-empty"):
        TopicProse(title=title, overview=overview)


def test_prose_of_answers_for_a_subject_that_declares_none() -> None:
    """Optional on the capability side: a secret backend satisfies a
    Protocol and derives from no base that could carry a default."""

    class Bare:
        pass

    assert prose_of(Bare) is None
    assert prose_of(object()) is None


def test_prose_of_reads_a_declared_record() -> None:
    class Documented:
        prose = PROSE

    assert prose_of(Documented) is PROSE


def test_prose_of_ignores_an_attribute_that_is_not_prose() -> None:
    """A ``prose`` attribute of some other type is a mistake, not prose to
    render: reporting none is what keeps a renderer from formatting it."""

    class Confused:
        prose = "just a string"

    assert prose_of(Confused) is None
