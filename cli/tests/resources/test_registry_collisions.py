"""``Registry.add`` collision handling (resource-manifests SDD, Phase 2).

Silent last-writer-wins is gone: operator-vs-operator collisions error
citing both locations, operator-vs-built-in consults the kind's
``builtin_override`` flag, and built-in republish stays idempotent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.errors import ConfigError
from agentworks.resources import Origin, Registry
from agentworks.secrets.base import SecretDecl
from agentworks.source_location import SourceLocation


def _decl(name: str) -> SecretDecl:
    return SecretDecl(
        name=name,
        description="d",
        declared_at=SourceLocation(file=Path("x.toml"), line=1),
    )


def _operator(line: int) -> Origin:
    return Origin.operator_declared(file=Path(f"f{line}.yaml"), line=line)


def _system_plugin(plugin: str) -> Origin:
    return Origin.system_plugin(plugin=plugin, source=f"agentworks.plugins.{plugin}")


def test_operator_over_operator_errors_with_both_locations() -> None:
    registry = Registry.empty()
    registry.add("secret", "s1", _decl("s1"), _operator(1))
    with pytest.raises(ConfigError) as exc:
        registry.add("secret", "s1", _decl("s1"), _operator(9))
    message = str(exc.value)
    assert "duplicate secret" in message
    assert "f1.yaml:1" in message
    assert "f9.yaml:9" in message


def test_operator_over_reserved_builtin_errors() -> None:
    registry = Registry.empty()
    registry.add("secret", "s1", _decl("s1"), Origin.built_in(source="app"))
    with pytest.raises(ConfigError) as exc:
        registry.add("secret", "s1", _decl("s1"), _operator(3))
    message = str(exc.value)
    assert "reserved" in message
    # The incoming (operator) declaration's file:line locator, matching the
    # operator-over-operator sibling branch, so the operator can find it.
    assert "f3.yaml:3" in message


def test_operator_over_allow_builtin_replaces() -> None:
    from agentworks.apt import AptPackageEntry

    registry = Registry.empty()
    entry = AptPackageEntry(name="gh", description="builtin", apt=["gh"])
    registry.add("apt-package", "gh", entry, Origin.built_in(source="app"))
    override = AptPackageEntry(name="gh", description="operator", apt=["gh2"])
    registry.add("apt-package", "gh", override, _operator(5))
    assert registry.lookup("apt-package", "gh").description == "operator"


def test_builtin_republish_is_idempotent() -> None:
    registry = Registry.empty()
    registry.add("secret", "s1", _decl("s1"), Origin.built_in(source="app"))
    registry.add("secret", "s1", _decl("s1"), Origin.built_in(source="app"))
    assert registry.lookup("secret", "s1").origin.variant == "built-in"


def test_builtin_over_operator_is_an_ordering_conflict() -> None:
    registry = Registry.empty()
    registry.add("secret", "s1", _decl("s1"), _operator(1))
    with pytest.raises(ConfigError):
        registry.add("secret", "s1", _decl("s1"), Origin.built_in(source="app"))


def test_operator_collision_has_no_singleton_exemption() -> None:
    """The synthesized-singleton exemption is gone: the TOML publisher no
    longer publishes placeholder rows for omitted sections (the framework
    auto-declares instead), so ANY operator-vs-operator collision is a
    duplicate -- including admin-template with a line-0 origin."""
    registry = Registry.empty()
    line_zero = Origin.operator_declared(file=Path("config.toml"), line=0)
    registry.add("admin-template", "default", _decl("default"), line_zero)
    with pytest.raises(ConfigError):
        registry.add("admin-template", "default", _decl("default"), _operator(7))


def test_line_zero_origin_collides_on_every_kind() -> None:
    registry = Registry.empty()
    line_zero = Origin.operator_declared(file=Path("config.toml"), line=0)
    registry.add("apt-package", "tool", _decl("tool"), line_zero)
    with pytest.raises(ConfigError):
        registry.add("apt-package", "tool", _decl("tool"), _operator(3))


# -- The R7 system-plugin matrix (declarable rows + operator override) ------
#
# Capability clashes never reach _check_collision (the seating guard in
# register_plugin owns them), so these drive DECLARABLE rows. The two
# directions of each system-plugin pairing share one message (the unordered
# normalization); the pre-existing built-in/operator directional asymmetry is
# preserved verbatim by the tests above (reserved one way, ordering-conflict
# the other).


def test_two_system_plugins_on_one_name_is_a_curation_error() -> None:
    registry = Registry.empty()
    registry.add("secret", "s1", _decl("s1"), _system_plugin("alpha"))
    with pytest.raises(ConfigError):
        registry.add("secret", "s1", _decl("s1"), _system_plugin("beta"))


def test_system_plugin_over_builtin_collides() -> None:
    registry = Registry.empty()
    registry.add("secret", "s1", _decl("s1"), Origin.built_in(source="app"))
    with pytest.raises(ConfigError):
        registry.add("secret", "s1", _decl("s1"), _system_plugin("alpha"))


def test_builtin_over_system_plugin_collides_same_message() -> None:
    registry = Registry.empty()
    registry.add("secret", "s1", _decl("s1"), _system_plugin("alpha"))
    with pytest.raises(ConfigError):
        registry.add("secret", "s1", _decl("s1"), Origin.built_in(source="app"))


def test_operator_over_reserved_system_plugin_errors() -> None:
    registry = Registry.empty()
    registry.add("secret", "s1", _decl("s1"), _system_plugin("alpha"))
    with pytest.raises(ConfigError):
        registry.add("secret", "s1", _decl("s1"), _operator(3))


def test_operator_over_allow_system_plugin_replaces() -> None:
    from agentworks.apt import AptPackageEntry

    registry = Registry.empty()
    entry = AptPackageEntry(name="gh", description="plugin", apt=["gh"])
    registry.add("apt-package", "gh", entry, _system_plugin("alpha"))
    override = AptPackageEntry(name="gh", description="operator", apt=["gh2"])
    registry.add("apt-package", "gh", override, _operator(5))
    assert registry.lookup("apt-package", "gh").description == "operator"


def test_enabled_plugin_over_operator_allow_kind_keeps_operator_no_error() -> None:
    # BLOCKING 1 (Phase 7): the override is SYMMETRIC on an allow-kind. An
    # enabled plugin's strong row landing over an operator's own declaration
    # (the operator-first order: deprecated TOML publisher runs before
    # publish_plugins) must let the OPERATOR win with NO error (KEEP_EXISTING),
    # so an operator's legacy `[system_install_commands] az-cli` is not broken
    # when they enable the plugin shipping that name. This replaces the old
    # "reverse direction always errors" behavior.
    from agentworks.apt import AptPackageEntry

    registry = Registry.empty()
    operator_row = AptPackageEntry(name="gh", description="operator", apt=["gh"])
    registry.add("apt-package", "gh", operator_row, _operator(5))
    plugin_row = AptPackageEntry(name="gh", description="plugin", apt=["gh2"])
    # No error, and the operator's row stands untouched (plugin row dropped).
    registry.add("apt-package", "gh", plugin_row, _system_plugin("alpha"))
    assert registry.lookup("apt-package", "gh").description == "operator"
    assert registry.lookup("apt-package", "gh").origin.variant == "operator-declared"


def test_enabled_plugin_over_operator_reserved_kind_still_errors() -> None:
    # On a RESERVED kind the symmetry preserves the error: a plugin cannot
    # shadow an operator's reserved declarable, in either encounter order.
    registry = Registry.empty()
    registry.add("secret", "s1", _decl("s1"), _operator(5))
    with pytest.raises(ConfigError):
        registry.add("secret", "s1", _decl("s1"), _system_plugin("alpha"))
    assert registry.lookup("secret", "s1").origin.variant == "operator-declared"
