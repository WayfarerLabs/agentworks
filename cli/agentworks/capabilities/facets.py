"""Facets: the levels a capability is driven at.

A facet names the pairing of one level's API methods and that level's
config, and nothing more. A capability offers a fixed set of facet
configs the same way it offers a fixed set of API methods, and CONSUMERS
choose which facet they drive, so a producer never has to know who is
asking.

**Facets are deliberately not scopes, and core owns the mapping between
them.** The platform's scopes are vm, admin, agent, workspace, and
session; the facets are the four below. Admin and agent both resolve to
:attr:`Facet.USER`, and session start and resume share
:attr:`Facet.SESSION`, so a vm-template's admin attachment and an agent
template get the same answer BY CONSTRUCTION rather than by each
capability encoding that the two mean the same thing. That mapping lives
in the core code that drives each level; nothing under ``capabilities/``
spells a scope.

**Config presence is not a support claim.** Asking a capability for its
config at a facet asks what SHAPE the config has there, never whether the
capability implements that level. Support is carried by the
implementation (the kind's base supplies no-op defaults and an
implementation overrides what it supports), and a capability may support
a facet while offering no config there. Wiring the two together would
rebuild the declaration-contract mechanism that was rescinded on
2026-08-05, under a new name.

Wave 2 names no facet: every shipped capability offers one config shared
by all of its operations, so it declares ``config_model`` and every core
call site passes ``None``. The vocabulary exists so that the first
capability whose methods run at several levels (a harness integration,
wave 4) adds its per-facet declaration and its consumers' facets in one
change, rather than needing a framework change first.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel


class Facet(Enum):
    """The level a capability is driven at.

    Fixed and core-owned: there is no facet registry, and a change to
    this set is an ordinary contract change rather than a framework
    event.
    """

    VM = "vm"
    """The VM itself: initialization and reinitialization."""

    USER = "user"
    """A user on a VM. Both the admin user (during VM init) and each
    agent (during agent init) resolve here, which is the whole reason
    the facet is not the scope."""

    WORKSPACE = "workspace"
    """A workspace, initialized once at create."""

    SESSION = "session"
    """A running session, at start and at resume alike."""


def facet_config(
    offered: Mapping[Facet, type[BaseModel]],
    facet: Facet | None,
    *,
    capability: str,
) -> type[BaseModel]:
    """``offered[facet]``, or a hard error naming what IS offered.

    The one line a capability writes when its methods run at several
    levels with different config; core owns the resolution so every kind
    refuses alike and the message is the same wherever it comes from.

    ``StateError``, not ``ConfigError``: an unoffered facet means a
    CONSUMER asked a producer for a level it does not serve, which is a
    framework mistake that review and typing should have caught, not
    something an operator wrote.
    """
    if facet is not None and facet in offered:
        return offered[facet]
    available = ", ".join(sorted(member.value for member in offered))
    asked = facet.value if facet is not None else "no facet"
    raise StateError(
        f"capability {capability!r} was asked for its config at {asked}, but it offers config at: {available}"
    )
