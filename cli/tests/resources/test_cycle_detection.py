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
    """Test-only Resource: publishes zero or one outgoing requirements.
    ``target_name=None`` produces a leaf (no outgoing edges) for the
    auto-declare synthesize stub.
    """

    self_name: str
    target_name: str | None = None
    origin: Origin | None = None
    usage: tuple = ()

    def required_resources(self) -> tuple[ResourceRequirement, ...]:
        if self.target_name is None:
            return ()
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
    r.add("node", name, _NodeWithReq(self_name=name, target_name=target), _opdecl())


@pytest.fixture()
def node_kind_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the ``_StubKind`` in ``KIND_REGISTRY`` for the duration of
    a test. ``monkeypatch.setitem`` restores the prior state automatically,
    which is safer than try/finally if test execution is ever parallelized.
    """
    from agentworks.resources.kind import KIND_REGISTRY
    monkeypatch.setitem(KIND_REGISTRY, "node", _StubKind())


def test_two_node_cycle_detected(node_kind_registered: None) -> None:
    r = Registry.empty()
    _add_node(r, "a", "b")
    _add_node(r, "b", "a")
    with pytest.raises(ConfigError, match="cycle"):
        r.finalize()


def test_three_node_cycle_detected(node_kind_registered: None) -> None:
    r = Registry.empty()
    _add_node(r, "a", "b")
    _add_node(r, "b", "c")
    _add_node(r, "c", "a")
    with pytest.raises(ConfigError, match="cycle"):
        r.finalize()


def test_acyclic_chain_does_not_error(node_kind_registered: None) -> None:
    r = Registry.empty()
    # a -> b -> c, no cycle (c gets auto-declared as a leaf)
    _add_node(r, "a", "b")
    _add_node(r, "b", "c")
    r.finalize()
    # c is present post-finalize via auto-declare; its synthesize stub
    # produces a leaf (target_name=None), so the chain terminates.
    assert r.lookup("node", "c") is not None


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
            self_name=first.name,
            target_name=None,  # leaf: no outgoing edges
            origin=Origin.auto_declared(source=first.source),
        )
