"""The tmux-layout choice set, and its one authored home.

``VALID_TMUX_LAYOUTS`` used to be the authored list and the type did not
exist. The direction is inverted now, because a ``Literal`` cannot be
built from a runtime tuple while a tuple can be derived from a
``Literal``: that is what puts the choices into emitted JSON Schema and
into what ``agw resource describe`` can list, instead of leaving them
inside a validator nobody outside this module can see.
"""

from __future__ import annotations

from typing import get_args

from agentworks.sessions.layouts import AW_SESSION_VERTICAL_LAYOUT, VALID_TMUX_LAYOUTS, TmuxLayout


def test_the_tuple_is_derived_from_the_type() -> None:
    assert get_args(TmuxLayout) == VALID_TMUX_LAYOUTS


def test_the_agentworks_layout_is_one_of_the_choices() -> None:
    """The one value spelled twice in the module: once as the constant the
    layout builder switches on, once inside the ``Literal``."""
    assert AW_SESSION_VERTICAL_LAYOUT in VALID_TMUX_LAYOUTS
