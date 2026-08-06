"""The facet vocabulary and its resolution.

A facet is the level a capability is driven at, and consumers choose
which one they drive. Wave 2 names no facet (every shipped capability
offers one config shared by all of its operations), so what is pinned
here is the CONTRACT the first multi-level capability relies on: a
per-facet offering answers each facet it has and refuses, by name, one it
does not.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agentworks.capabilities.facets import Facet, facet_config
from agentworks.errors import StateError
from agentworks.resources.schema import AgwModel


class UserConfig(AgwModel):
    """The config a capability's user-level methods take."""

    home: str | None = None


class SessionConfig(AgwModel):
    """The config a capability's session-level methods take."""

    command: str | None = None


OFFERED: dict[Facet, type[BaseModel]] = {Facet.USER: UserConfig, Facet.SESSION: SessionConfig}


def test_the_facets_are_the_four_levels_and_no_more() -> None:
    """Fixed and core-owned: there is no facet registry, and a change to
    this set is an ordinary contract change."""
    assert {facet.value for facet in Facet} == {"vm", "user", "workspace", "session"}


@pytest.mark.parametrize(("facet", "expected"), [(Facet.USER, UserConfig), (Facet.SESSION, SessionConfig)])
def test_each_offered_facet_answers_with_its_own_config(facet: Facet, expected: type[BaseModel]) -> None:
    assert facet_config(OFFERED, facet, capability="claude-code") is expected


def test_an_unoffered_facet_is_a_hard_error_naming_what_is_offered() -> None:
    with pytest.raises(StateError) as caught:
        facet_config(OFFERED, Facet.VM, capability="claude-code")

    message = str(caught.value)
    assert "claude-code" in message
    assert "vm" in message
    assert "session, user" in message, "the error has to say what the capability DOES offer"


def test_asking_a_per_facet_capability_for_no_facet_is_the_same_hard_error() -> None:
    """A consumer that names no facet has not chosen, which a capability
    with several cannot answer for it."""
    with pytest.raises(StateError, match="no facet"):
        facet_config(OFFERED, None, capability="claude-code")
