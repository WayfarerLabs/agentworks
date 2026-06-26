"""Tests for cycle detection in ``Registry.finalize()``.

Phase 1 doesn't exercise cycles via real producers (secrets don't reference
secrets), so we synthesize a Resource type that produces cycle-shaped
requirements. Phase 2's template-inheritance work is where this check earns
its keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from agentworks.errors import ConfigError
from agentworks.resources import Origin, Registry
from agentworks.resources.requirement import ResourceRequirement


@dataclass(frozen=True)
class _NodeWithReq:
    """Test-only Resource: publishes one requirement pointing at another node."""

    target_name: str
    self_name: str
    origin: Origin | None = None
    usage: tuple = ()

    def required_resources(self) -> tuple[ResourceRequirement, ...]:
        return (
            ResourceRequirement(
                name=self.target_name,
                kind="node",
                usage="next link",
                source=("node", self.self_name),
            ),
        )


def _opdecl() -> Origin:
    return Origin.operator_declared(file=Path("/x.toml"), line=1)


def _add_node(r: Registry, name: str, target: str) -> None:
    r.add("node", name, _NodeWithReq(target_name=target, self_name=name), _opdecl())


def test_two_node_cycle_detected() -> None:
    r = Registry.empty()
    # Register a custom kind so finalize doesn't error on unknown "node" kind.
    from agentworks.resources.kind import KIND_REGISTRY
    KIND_REGISTRY["node"] = _StubKind()
    try:
        _add_node(r, "a", "b")
        _add_node(r, "b", "a")
        with pytest.raises(ConfigError, match="cycle"):
            r.finalize()
    finally:
        del KIND_REGISTRY["node"]


def test_three_node_cycle_detected() -> None:
    r = Registry.empty()
    from agentworks.resources.kind import KIND_REGISTRY
    KIND_REGISTRY["node"] = _StubKind()
    try:
        _add_node(r, "a", "b")
        _add_node(r, "b", "c")
        _add_node(r, "c", "a")
        with pytest.raises(ConfigError, match="cycle"):
            r.finalize()
    finally:
        del KIND_REGISTRY["node"]


def test_acyclic_chain_does_not_error() -> None:
    r = Registry.empty()
    from agentworks.resources.kind import KIND_REGISTRY
    KIND_REGISTRY["node"] = _StubKind()
    try:
        # a -> b -> c, no cycle (c has no outgoing edge in this test)
        _add_node(r, "a", "b")
        _add_node(r, "b", "c")
        # c gets auto-declared by the miss policy
        r.finalize()
        # c is present in the registry post-finalize via auto-declare
        assert r.lookup("node", "c") is not None
    finally:
        del KIND_REGISTRY["node"]


@dataclass(frozen=True)
class _StubKind:
    """Test-only ResourceKind: synthesizes a no-outgoing-edges node so that
    a chain ending in an unpublished name can finalize cleanly.
    """

    kind: str = "node"
    miss_policy: str = "auto-declare"
    auto_declare_names: frozenset[str] | None = None

    def synthesize(self, requirements):
        first = requirements[0]
        return _NodeWithReq(
            target_name="__leaf__",
            self_name=first.name,
            origin=Origin.auto_declared(source=first.source),
            usage=tuple(),
        )
