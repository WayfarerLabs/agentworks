"""The memoized multi-root walk over declared edges.

The one traversal mechanism every orchestrator uses (FRD R4): which
nodes to root the walk at, and when, is each orchestrator's call; HOW
the graph is traversed is decided once, here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import StateError

if TYPE_CHECKING:
    from agentworks.orchestration.node import Node


def walk(*roots: Node) -> tuple[Node, ...]:
    """Post-order walk of the declared dependency graph under ``roots``:
    dependencies before dependents, each node exactly once.

    Multi-root from day one (batch commands root at many nodes; spike
    finding 2): memoization is shared across roots, so a node reached
    from several roots, or by several paths under one root (a shared
    ``git-credential`` under two consumers), is visited once. Order is
    deterministic: depth-first in ``roots`` order, each node's declared
    ``deps()`` order.

    Loud errors, never silent repair (spike finding 3: keys do real
    work, so key bugs must not be absorbed):

    - a dependency CYCLE raises :class:`StateError` naming the chain;
    - two DISTINCT objects sharing one key raise :class:`StateError`.
      One-object-per-key is the node-construction contract (every edge
      holder must observe the same object the orchestrator later marks
      realized); two constructions of "the same" node is a factory bug
      worth failing on, not deduplicating over.
    """
    order: list[Node] = []
    seen: dict[str, Node] = {}
    visiting: dict[str, Node] = {}

    def check_identity(node: Node, prior: Node) -> None:
        if prior is not node:
            raise StateError(
                f"two distinct node objects share the key "
                f"'{node.key}'. Nodes are constructed once per command "
                f"and shared by object (memoized by key at "
                f"construction); a duplicate construction breaks the "
                f"one-object-per-node contract."
            )

    def visit(node: Node) -> None:
        done = seen.get(node.key)
        if done is not None:
            check_identity(node, done)
            return
        in_progress = visiting.get(node.key)
        if in_progress is not None:
            check_identity(node, in_progress)
            chain = " -> ".join((*visiting, node.key))
            raise StateError(f"node dependency cycle: {chain}")
        visiting[node.key] = node
        for dep in node.deps():
            visit(dep)
        del visiting[node.key]
        seen[node.key] = node
        order.append(node)

    for root in roots:
        visit(root)
    return tuple(order)
