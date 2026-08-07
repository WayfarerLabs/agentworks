"""Cycle-safe descent over graphs whose size and shape an operator chooses.

An inheritance chain, a reference graph, and a nested blob are all written
by an operator, so a walker over one may not assume it is finite, shallow,
or acyclic. A hand-rolled recursive descent over any of them carries two
bugs: it never returns on a cycle, and it raises ``RecursionError`` on a
chain deeper than CPython's limit. Both walks here are therefore
ITERATIVE, and both carry an explicit guard (operator agreement,
2026-08-07; see ``docs/sdd/2026-08-04-next-steps/target-state.md``, under
"Cross-cutting: shared traversal discipline").

A bounded walk over CODE-shaped structure, a model class's own declared
fields, may still be spelled recursively: its depth is the model graph's,
which an author writes and a reader can count. The discipline is for
input whose size is the operator's to choose.

Which walk a caller wants turns on ONE question: does reaching a node a
second time, by a different route, mean something?

- :func:`iter_descendants` says YES. Two sibling fields holding the same
  nested model each describe a block an operator writes, and a walk that
  visited the model once would silently drop the second. It cuts the one
  case that cannot terminate on its own: a node already being expanded
  further up the current path.
- :func:`iter_post_order` says NO. A template inherited through two
  routes is one template: expanding it per route is what turns a diamond
  ladder exponential, and applying it per route is what lets a
  grandparent's value land on top of the parent that overrode it.

Neither raises on its own. :func:`iter_post_order` takes an ``on_cycle``
hook for the callers that must refuse one, because whether a cycle is
fatal belongs to the caller and not to the plumbing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable, Iterable, Iterator

#: The frame that holds the root, so the root needs no separate handling
#: and so the loop below has exactly one place that yields. Never a key of
#: a real node, and removing it from the path set is a no-op.
_ROOT_FRAME: Final = object()


def iter_descendants[N](
    root: N,
    successors: Callable[[N], Iterable[N]],
    *,
    key: Callable[[N], Hashable],
) -> Iterator[N]:
    """``root`` and everything under it, depth first, in ``successors``
    order, cutting only cycles.

    The guard is the current PATH, not an accumulating visited set: a node
    is skipped exactly when it is already being expanded above this point,
    which is the one case that cannot terminate on its own. A node reached
    again along a different route is walked again, because for these
    callers that is a different thing to describe.

    ``key`` says what "the same node" means, and it is required rather
    than defaulted because that choice is the whole decision the caller is
    making. Reference extraction keys a node on the DATA it reads as well
    as the model it reads it against, so that finite nested data walks to
    its bottom and only a value reachable from itself terminates; keying
    on the model alone would truncate the walk at the first repeated type
    and drop real edges.

    ``successors`` is called at most once per yielded node, and it is
    called AFTER the caller's work for that node, so a caller may emit
    from it and keep document order.
    """
    on_path: set[Hashable] = set()
    frames: list[tuple[Hashable, Iterator[N]]] = [(_ROOT_FRAME, iter((root,)))]
    while frames:
        frame_key, pending = frames[-1]
        try:
            node = next(pending)
        except StopIteration:
            on_path.discard(frame_key)
            frames.pop()
            continue
        node_key = key(node)
        if node_key in on_path:
            continue
        on_path.add(node_key)
        yield node
        frames.append((node_key, iter(successors(node))))


def iter_post_order[N: Hashable](
    root: N,
    successors: Callable[[N], Iterable[N]],
    *,
    on_cycle: Callable[[tuple[N, ...]], None] | None = None,
) -> Iterator[N]:
    """Every node reachable from ``root``, each yielded ONCE, after all of
    its successors, depth first in ``successors`` order.

    The nodes ARE their own identity, which is what distinguishes this
    from :func:`iter_descendants`: this walk is for a graph whose nodes
    are names (or another hashable address), where reaching one twice
    means the same node twice.

    That is a LINEARIZATION, and both halves of it are load-bearing for
    the caller that folds the result. Every node lands after everything it
    depends on, and it lands ONCE, at the earliest position the depth-first
    order allows. Yielding it again at each later route would put a node
    back on top of the more specific nodes that were meant to override it,
    and would make the output grow by a factor of two per diamond: an
    inheritance ladder of 55 rows produced 1,048,573 layers that way.

    ``on_cycle`` is called with the open path, the repeated node appended,
    the first time an edge closes a cycle. Raise from it to refuse the
    input; leave it ``None`` and the edge is simply not followed, which is
    what a caller running somewhere that may not raise wants. The walk is
    iterative either way, so a chain deeper than CPython's recursion limit
    is answered rather than crashed into.
    """
    done: set[N] = set()
    open_nodes: list[N] = []
    on_path: set[N] = set()
    frames: list[Iterator[N]] = [iter((root,))]
    while frames:
        try:
            node = next(frames[-1])
        except StopIteration:
            frames.pop()
            if open_nodes:
                # The frame that just emptied belongs to the deepest open
                # node, so that node's successors are all done and it is
                # its turn. The root's own pseudo-frame empties last, with
                # nothing left open under it.
                finished = open_nodes.pop()
                on_path.discard(finished)
                done.add(finished)
                yield finished
            continue
        if node in done:
            continue
        if node in on_path:
            if on_cycle is not None:
                on_cycle((*open_nodes, node))
            continue
        open_nodes.append(node)
        on_path.add(node)
        frames.append(iter(successors(node)))
