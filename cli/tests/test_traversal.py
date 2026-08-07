"""The shared cycle-safe walks, against graphs an operator could write.

Two properties carry everything built on this module, and neither is
visible in a small example: the walks TERMINATE on any graph, and they do
so without recursing, because an inheritance chain or a nested document is
as deep as its author made it. Both are asserted at a size CPython's
recursion limit refuses.

:func:`iter_post_order`'s output is also pinned against a brute-force
reference implementation over random graphs, rather than against hand
written expectations. The order is the thing four resolvers fold and every
provenance answer is read off, and a hand-written expectation would only
say that the code does what the code does.
"""

from __future__ import annotations

import random
import sys

import pytest

from agentworks.traversal import iter_descendants, iter_post_order


def _graph_successors(edges: dict[str, list[str]]):
    def successors(node: str) -> list[str]:
        return edges.get(node, [])

    return successors


# --- iter_descendants: the path-scoped walk --------------------------------


def test_a_node_reached_by_two_routes_is_walked_by_each() -> None:
    """The property that separates this walk from the other one: two
    sibling fields holding the same nested model are two blocks an
    operator writes."""
    edges = {"root": ["left", "right"], "left": ["shared"], "right": ["shared"]}
    walked = list(iter_descendants("root", _graph_successors(edges), key=lambda node: node))

    assert walked == ["root", "left", "shared", "right", "shared"]


def test_a_cycle_is_cut_at_the_node_already_on_the_path() -> None:
    edges = {"a": ["b"], "b": ["c"], "c": ["a", "d"]}
    walked = list(iter_descendants("a", _graph_successors(edges), key=lambda node: node))

    assert walked == ["a", "b", "c", "d"]


def test_the_key_decides_what_counts_as_the_same_node() -> None:
    """A walk over VALUES rather than over names: the same label at two
    different places is two nodes, which is exactly why reference
    extraction keys on the blob and not on the model."""
    nodes = [("m", 0), ("m", 1), ("m", 2)]
    successors = {node: [nodes[index + 1]] for index, node in enumerate(nodes[:-1])}
    walked = list(iter_descendants(nodes[0], lambda node: successors.get(node, []), key=lambda node: node))

    assert walked == nodes


def test_a_chain_deeper_than_the_recursion_limit_is_walked() -> None:
    depth = sys.getrecursionlimit() * 4
    edges = {str(level): [str(level + 1)] for level in range(depth)}
    walked = list(iter_descendants("0", _graph_successors(edges), key=lambda node: node))

    assert len(walked) == depth + 1


# --- iter_post_order: the linearizing walk ---------------------------------


def _reference_layers(edges: dict[str, list[str]], root: str) -> list[str]:
    """The naive recursive descent this walk replaces: each successor's
    own chain, then the node, with a path guard and no memo.

    Deliberately the shape the finding condemns, so the assertions below
    compare against something written independently of the code under
    test rather than against the code under test's own answer.
    """
    out: list[str] = []

    def collect(node: str, visiting: tuple[str, ...]) -> None:
        if node in visiting:
            return
        for parent in edges.get(node, []):
            collect(parent, (*visiting, node))
        out.append(node)

    collect(root, ())
    return out


def _keep_first(order: list[str]) -> list[str]:
    return list(dict.fromkeys(order))


@pytest.mark.parametrize("seed", range(40))
def test_the_order_is_the_naive_chain_with_every_repeat_dropped(seed: int) -> None:
    """Over random small graphs, cycles included.

    The walk must produce the naive descent's order with each node kept at
    its FIRST position. First, not last: a node's earliest position is
    before everything that reaches it, so everything more specific still
    gets to override it, and it is the position at which a union merge
    appends in the same order the naive chain did.
    """
    rng = random.Random(seed)  # noqa: S311
    names = [f"n{index}" for index in range(8)]
    edges = {name: rng.sample(names, rng.randint(0, 3)) for name in names}

    walked = list(iter_post_order("n0", _graph_successors(edges)))

    assert walked == _keep_first(_reference_layers(edges, "n0"))


def test_the_random_graphs_actually_contain_repeats() -> None:
    """Non-vacuity for the property above: if no generated graph had a
    diamond, it would be comparing two identical lists."""
    rng = random.Random(0)  # noqa: S311
    names = [f"n{index}" for index in range(8)]
    reduced = 0
    for _ in range(40):
        edges = {name: rng.sample(names, rng.randint(0, 3)) for name in names}
        naive = _reference_layers(edges, "n0")
        reduced += len(naive) > len(_keep_first(naive))

    assert reduced > 10


def test_a_diamond_ladder_is_linear_rather_than_exponential() -> None:
    """The measured symptom: the naive descent turned 55 rows into
    1,048,573 layers, in the finalize pass that runs BEFORE the cycle
    detector it was meant to be protected by."""
    levels = 20
    edges: dict[str, list[str]] = {}
    for level in range(levels):
        edges[f"kid{level}"] = [f"left{level}", f"right{level}"]
        parent = f"kid{level + 1}" if level + 1 < levels else "root"
        edges[f"left{level}"] = [parent]
        edges[f"right{level}"] = [parent]

    walked = list(iter_post_order("kid0", _graph_successors(edges)))

    assert len(walked) == len(edges) + 1


def test_a_chain_deeper_than_the_recursion_limit_is_linearized() -> None:
    depth = sys.getrecursionlimit() * 4
    edges = {str(level): [str(level + 1)] for level in range(depth)}
    walked = list(iter_post_order("0", _graph_successors(edges)))

    assert walked == [str(level) for level in reversed(range(depth + 1))]


def test_a_cycle_is_reported_to_the_hook_and_otherwise_ignored() -> None:
    """Whether a cycle is fatal belongs to the caller: one of them runs
    where nothing may raise, and the other has somebody to tell."""
    edges = {"a": ["b"], "b": ["c"], "c": ["a"]}
    seen: list[tuple[str, ...]] = []

    walked = list(iter_post_order("a", _graph_successors(edges), on_cycle=seen.append))

    assert walked == ["c", "b", "a"]
    assert seen == [("a", "b", "c", "a")]


def test_a_hook_that_raises_refuses_the_input() -> None:
    edges = {"a": ["b"], "b": ["a"]}

    def refuse(chain: tuple[str, ...]) -> None:
        raise ValueError(" -> ".join(chain))

    with pytest.raises(ValueError, match="a -> b -> a"):
        list(iter_post_order("a", _graph_successors(edges), on_cycle=refuse))


def test_every_node_lands_after_the_nodes_it_reaches() -> None:
    """The invariant a fold rests on, stated directly: a layer may only be
    overridden by something more specific than it."""
    rng = random.Random(7)  # noqa: S311
    names = [f"n{index}" for index in range(9)]
    # Edges point strictly forward, so the graph is acyclic and every
    # ordering question has one right answer.
    edges = {name: rng.sample(names[index + 1 :], min(2, len(names) - index - 1)) for index, name in enumerate(names)}
    walked = list(iter_post_order("n0", _graph_successors(edges)))
    position = {node: index for index, node in enumerate(walked)}

    for node, parents in edges.items():
        if node not in position:
            continue
        for parent in parents:
            assert position[parent] < position[node], f"{parent} must come before {node}"
