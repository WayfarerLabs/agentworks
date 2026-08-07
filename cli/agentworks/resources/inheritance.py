"""The ``inherits`` chain, as the framework sees it.

Four kinds inherit (``vm-template``, ``workspace-template``,
``agent-template``, ``session-template``) and each owns its own MERGE
semantics, which differ per field and stay in its resolver. What they
share is the SHAPE of the chain: an ordered list of declarations, parents
before the row itself, left to right. Three questions turn on that shape
alone, so they live here once rather than four times:

- which declarations a row's effective value is merged from, for a caller
  that may not raise (:func:`merge_layers`) and for one that may
  (:func:`resolution_layers`), and
- which of them declared a given key (:func:`declarers`), which is what
  lets an edge derived from a merged value name the file it actually came
  from rather than the row that happens to publish it (FR17).

The two layer functions are ONE walk with two cycle policies, and that is
deliberate: a provenance answer read off :func:`merge_layers` names the
layer whose value a resolver's fold actually kept, which is only true
while both see the same chain in the same order. The walk itself is
:mod:`agentworks.traversal`'s, so the pass that runs FIRST at finalize
cannot loop or overflow before the registry's canonical cycle report is
reachable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn, Protocol

from agentworks.errors import inheritance_cycle_error
from agentworks.traversal import iter_post_order

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence


class Inheriting(Protocol):
    """A declaration that composes other declarations of its own kind by
    name. The structural shape all four template dataclasses already
    have; nothing here reads any other field.

    Both members are spelled as read-only properties rather than as
    attributes, which is what a FROZEN dataclass satisfies: declaring
    them mutable would make every caller's row fail the bound, and the
    only reason production did not notice is that it passes rows out of
    the finalize context typed ``Any``.
    """

    @property
    def name(self) -> str: ...

    @property
    def inherits(self) -> list[str]: ...


def merge_layers[T: Inheriting](rows: Mapping[str, T], name: str) -> tuple[T, ...]:
    """The declarations ``name``'s effective value is merged from, in
    MERGE ORDER, for a caller that may NOT raise.

    A cyclic chain stops rather than raising, because this runs inside the
    finalize build walk; the registry's own pass is the canonical cycle
    report. :func:`resolution_layers` is the same walk for a caller that
    has somebody to tell.
    """
    return _layers(rows, name, on_cycle=None)


def resolution_layers[T: Inheriting](rows: Mapping[str, T], name: str, kind: str) -> tuple[T, ...]:
    """The declarations ``name``'s effective value is merged from, in
    MERGE ORDER, REFUSING a cyclic chain.

    What every kind's resolver folds. A resolve has a caller that can be
    told, so a loop raises ``InheritanceCycleError`` naming the chain,
    matching the framework's cycle-pass error shape. The canonical cycle
    check is ``Registry.finalize``'s, but a resolver is also called
    eagerly by ``load_config`` before any registry exists, so it needs its
    own answer.
    """
    return _layers(rows, name, on_cycle=lambda chain: _refuse(kind, chain))


def _layers[T: Inheriting](
    rows: Mapping[str, T],
    name: str,
    *,
    on_cycle: Callable[[tuple[str, ...]], None] | None,
) -> tuple[T, ...]:
    """The chain under both layer functions: each parent's own chain
    first (left to right), then the row itself.

    The order is the contract, not an implementation detail. A caller
    reads "the last layer that declared X" off it, so an order that
    diverged from the resolver's fold would attribute a value to a layer
    the fold overrode; each kind is pinned by a test that folds these
    layers and gets its resolver's own merged result.

    A layer reachable by more than one route appears ONCE, at its EARLIEST
    position, which is what makes this a linearization rather than a
    replay of the routes. Both halves matter. Repeating a layer put a
    grandparent's value back on top of the parent that had overridden it,
    so a ``kid`` inheriting two children of one ``root`` resolved to
    ``root``'s value for every field both declared, which is the same
    class of silent defect as a defaulted parent overwriting a declaring
    one. And repeating it grew the list by a factor of two per diamond:
    an inheritance ladder of 55 rows produced 1,048,573 layers.

    A name with no row contributes NO layer. An unresolved parent is the
    miss policy's to report, and a stand-in row would be a fabricated
    declaration whose every field says "the built-in default", which is
    the other entrance to that same silent defect.
    """
    walk = iter_post_order(name, lambda layer: _parents(rows, layer), on_cycle=on_cycle)
    return tuple(rows[layer] for layer in walk if layer in rows)


def _parents[T: Inheriting](rows: Mapping[str, T], name: str) -> tuple[str, ...]:
    """``name``'s declared parents, in declaration order. A name with no
    row has none, which is how an unresolved parent contributes nothing."""
    row = rows.get(name)
    return () if row is None else tuple(row.inherits)


def _refuse(kind: str, chain: tuple[str, ...]) -> NoReturn:
    raise inheritance_cycle_error(kind, chain)


def declarers[T: Inheriting](
    layers: Sequence[T],
    kind: str,
    keys: Callable[[T], Iterable[str]],
) -> dict[str, tuple[str, str]]:
    """Each key ``keys`` reads off a layer, mapped to the ``(kind, name)``
    of the LAST layer that declared it.

    Last, because that is the one a child-wins merge keeps, so the answer
    names the declaration whose value survived. For a field whose merge
    UNIONS instead (a list of packages), every contributor's value
    survives and the last is one truthful answer among several; what
    matters either way is that the named row really does contain the key,
    which holds by construction.
    """
    return {key: (kind, layer.name) for layer in layers for key in keys(layer)}
