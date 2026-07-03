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
    with pytest.raises(ConfigError, match="reserved"):
        registry.add("secret", "s1", _decl("s1"), _operator(3))


def test_operator_over_allow_builtin_replaces() -> None:
    from agentworks.catalog import AptPackageEntry

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


def test_builtin_over_operator_is_a_publisher_bug() -> None:
    registry = Registry.empty()
    registry.add("secret", "s1", _decl("s1"), _operator(1))
    with pytest.raises(AssertionError, match="publisher ordering"):
        registry.add("secret", "s1", _decl("s1"), Origin.built_in(source="app"))
