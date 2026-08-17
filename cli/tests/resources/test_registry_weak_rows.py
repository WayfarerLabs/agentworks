"""Weak (add-if-absent) manifest rows and the ``_CollisionDecision`` model
(Phase 7, LLD c 3b.3).

A not-enabled plugin's bundled declarable row is published ``weak``: it must
never block a stronger row (operator, built-in, or enabled plugin) in ANY
encounter order, while never erroring. These drive ``Registry.add(..., weak=)``
directly (the semantics ``publish_plugins`` relies on), plus the
weak-implies-disabled ``finalize`` guard.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.apt import AptPackageEntry
from agentworks.errors import ConfigError, StateError
from agentworks.plugins import plugin_enablement_source
from agentworks.resources import Origin, Registry

if TYPE_CHECKING:
    from agentworks.config import Config


def _entry(desc: str) -> AptPackageEntry:
    return AptPackageEntry(name="gh", description=desc, apt=["gh"])


def _operator() -> Origin:
    return Origin.operator_declared(file=Path("op.yaml"), line=1)


def _builtin() -> Origin:
    return Origin.built_in(source="app")


def _plugin(name: str) -> Origin:
    return Origin.system_plugin(plugin=name, source=f"agentworks.plugins.{name}")


def _config(*enabled: str) -> Config:
    return cast("Config", SimpleNamespace(enabled_system_plugins=tuple(enabled)))


# -- Weak incoming never displaces an occupant (any variant), no error ----------


@pytest.mark.parametrize("occupant", ["operator", "builtin", "plugin"])
def test_weak_incoming_over_occupied_slot_is_a_noop(occupant: str) -> None:
    origins = {"operator": _operator(), "builtin": _builtin(), "plugin": _plugin("alpha")}
    registry = Registry.empty()
    registry.add("apt-package", "gh", _entry("occupant"), origins[occupant])
    # A weak plugin row landing on the occupied slot is a silent no-op: no
    # collision check runs, nothing errors, the occupant stands.
    registry.add("apt-package", "gh", _entry("weak"), _plugin("beta"), weak=True)
    assert registry.lookup("apt-package", "gh").description == "occupant"


def test_weak_incoming_over_reserved_kind_occupant_does_not_error() -> None:
    # As-if-absent holds even on a reserved (builtin_override) occupant: a weak
    # row never triggers the reserved-name error against the existing row.
    from agentworks.secrets.base import SecretDecl
    from agentworks.source_location import SourceLocation

    decl = SecretDecl(name="s1", description="d", declared_at=SourceLocation(file=Path("x.toml"), line=1))
    registry = Registry.empty()
    registry.add("secret", "s1", decl, _operator())
    registry.add("secret", "s1", decl, _plugin("alpha"), weak=True)  # no raise
    assert registry.lookup("secret", "s1").origin.variant == "operator-declared"


# -- Weak incoming into a free slot lands and is recorded weak -------------------


def test_weak_into_free_slot_lands() -> None:
    # That the landed row is RECORDED weak is proven observably by
    # test_weak_survivor_with_no_source_is_a_state_error (the finalize guard
    # fires only for a recorded weak survivor), so this pins just the landing.
    registry = Registry.empty()
    registry.add("apt-package", "gh", _entry("weak"), _plugin("alpha"), weak=True)
    assert registry.lookup("apt-package", "gh").description == "weak"


# -- Strong incoming over an existing weak row replaces silently -----------------


@pytest.mark.parametrize("strong", ["operator", "builtin", "plugin"])
def test_strong_over_weak_replaces_silently(strong: str) -> None:
    origins = {"operator": _operator(), "builtin": _builtin(), "plugin": _plugin("alpha")}
    registry = Registry.empty()
    registry.add("apt-package", "gh", _entry("weak"), _plugin("beta"), weak=True)
    # A strong row replaces the weak one with no collision check; that the key
    # leaves the weak set is proven observably by
    # test_strong_replaced_weak_does_not_trip_the_guard (finalize stays clean
    # with no source).
    registry.add("apt-package", "gh", _entry("strong"), origins[strong])
    assert registry.lookup("apt-package", "gh").description == "strong"


# -- Two disabled (weak) plugins on one name: first wins, no error ---------------


def test_two_weak_rows_first_published_wins_no_error() -> None:
    # The acknowledged tradeoff: two DISABLED plugins sharing a name silently
    # keep the first-published row (no curation error while both are off).
    registry = Registry.empty()
    registry.add("apt-package", "gh", _entry("first"), _plugin("alpha"), weak=True)
    registry.add("apt-package", "gh", _entry("second"), _plugin("beta"), weak=True)
    assert registry.lookup("apt-package", "gh").description == "first"


# -- The weak-implies-disabled finalize guard -----------------------------------


def test_weak_survivor_with_no_source_is_a_state_error() -> None:
    # A weak row that finalize sees with NO disabling mark is a framework bug
    # (a publisher declared a row weak without a source to disable it).
    registry = Registry.empty()
    registry.add("apt-package", "gh", _entry("weak"), _plugin("alpha"), weak=True)
    with pytest.raises(StateError):
        registry.finalize()  # no enablement source -> the weak row is not disabled


def test_weak_survivor_disabled_by_source_finalizes_clean() -> None:
    # A weak row disabled by the plugin source finalizes fine and is
    # present-but-disabled.
    from agentworks.resources.graph import Enablement

    registry = Registry.empty()
    registry.add("apt-package", "gh", _entry("weak"), _plugin("alpha"), weak=True)
    registry.finalize(enablement_sources=[plugin_enablement_source(_config())])  # alpha not enabled
    assert registry.graph.enablement_of("apt-package", "gh") is Enablement.disabled


def test_strong_replaced_weak_does_not_trip_the_guard() -> None:
    # A weak row later replaced by a strong (enabled) row must not trip the
    # guard: the key left the weak set on replacement, so finalize with no
    # source is clean.
    registry = Registry.empty()
    registry.add("apt-package", "gh", _entry("weak"), _plugin("alpha"), weak=True)
    registry.add("apt-package", "gh", _entry("operator"), _operator())
    registry.finalize()  # no StateError
    assert registry.lookup("apt-package", "gh").description == "operator"


# -- Two ENABLED plugins on one strong name still collide (curation bug) ---------


def test_two_enabled_plugin_strong_rows_still_collide() -> None:
    registry = Registry.empty()
    registry.add("apt-package", "gh", _entry("alpha"), _plugin("alpha"))
    with pytest.raises(ConfigError):
        registry.add("apt-package", "gh", _entry("beta"), _plugin("beta"))
