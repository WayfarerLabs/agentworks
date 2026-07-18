"""The memoized multi-root walk: order, dedup, loud errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.errors import StateError
from agentworks.orchestration.walk import walk

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.orchestration.node import Node


@dataclass
class _N:
    key: str
    _deps: tuple[_N, ...] = ()

    def deps(self) -> tuple[_N, ...]:
        return self._deps

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None: ...


def _keys(nodes: tuple[Node, ...]) -> list[str]:
    return [n.key for n in nodes]


def test_dependencies_come_before_dependents() -> None:
    c = _N("vm-site/px")
    b = _N("vm/box", (c,))
    a = _N("session/s1", (b,))
    assert _keys(walk(a)) == ["vm-site/px", "vm/box", "session/s1"]


def test_diamond_is_visited_once() -> None:
    """A node shared by two consumers (a git-credential under two
    agents) appears once, before both."""
    shared = _N("git-credential/gh")
    left = _N("agent/dev", (shared,))
    right = _N("agent/qa", (shared,))
    root = _N("vm/box", (left, right))
    assert _keys(walk(root)) == [
        "git-credential/gh",
        "agent/dev",
        "agent/qa",
        "vm/box",
    ]


def test_multi_root_shares_memoization() -> None:
    """Batch commands root at many nodes (``walk(*roots)`` from day
    one): a platform shared by two VMs is visited
    once across roots, reproducing ``bind_platforms``' by-site dedup."""
    site = _N("vm-site/px")
    vm1 = _N("vm/box1", (site,))
    vm2 = _N("vm/box2", (site,))
    assert _keys(walk(vm1, vm2)) == ["vm-site/px", "vm/box1", "vm/box2"]


def test_deterministic_order_follows_roots_then_deps() -> None:
    d1 = _N("git-credential/gh")
    d2 = _N("git-credential/ado")
    root = _N("agent/dev", (d1, d2))
    assert _keys(walk(root)) == [
        "git-credential/gh",
        "git-credential/ado",
        "agent/dev",
    ]


def test_cycle_is_a_loud_error() -> None:
    a = _N("vm/box")
    b = _N("workspace/ws1", (a,))
    # Close the loop after construction (dataclass field mutation).
    a._deps = (b,)
    with pytest.raises(StateError, match=r"cycle: vm/box -> workspace/ws1 -> vm/box"):
        walk(a)


def test_self_cycle_is_a_loud_error() -> None:
    a = _N("vm/box")
    a._deps = (a,)
    with pytest.raises(StateError, match="cycle"):
        walk(a)


def test_cycle_report_trims_the_acyclic_prefix() -> None:
    """A cycle entered through a non-cycle prefix reports only the
    cycle, not the path the walk happened to reach it through."""
    a = _N("workspace/ws1")
    b = _N("agent/dev", (a,))
    a._deps = (b,)
    root = _N("session/s1", (b,))
    with pytest.raises(
        StateError, match=r"cycle: agent/dev -> workspace/ws1 -> agent/dev$"
    ):
        walk(root)


def test_two_objects_sharing_a_key_is_a_loud_error() -> None:
    """One-object-per-key is the node-construction contract: a
    duplicate construction of 'the same' node would leave edge holders
    watching the wrong object, so the walk refuses it rather than
    deduplicating over it."""
    first = _N("git-credential/gh")
    second = _N("git-credential/gh")
    root = _N("vm/box", (first, second))
    with pytest.raises(StateError, match="share the key 'git-credential/gh'"):
        walk(root)


def test_two_objects_sharing_a_key_across_roots_is_loud_too() -> None:
    with pytest.raises(StateError, match="share the key"):
        walk(_N("vm/box"), _N("vm/box"))


def test_result_is_node_typed() -> None:
    """The fakes satisfy the ``Node`` protocol the walk is typed
    over."""
    from agentworks.orchestration.node import Node as NodeProtocol

    node = cast("Node", _N("vm/box"))
    (walked,) = walk(node)
    assert isinstance(walked, NodeProtocol)
