"""The ``inherits`` chain, as the framework sees it.

Four kinds inherit (``vm-template``, ``workspace-template``,
``agent-template``, ``session-template``) and each owns its own MERGE
semantics, which differ per field and stay in its resolver. What they
share is the SHAPE of the chain: an ordered list of declarations, parents
before the row itself, left to right. Two questions turn on that shape
alone, so they live here once rather than four times:

- which declarations a row's effective value was merged from
  (:func:`merge_layers`), and
- which of them declared a given key (:func:`declarers`), which is what
  lets an edge derived from a merged value name the file it actually came
  from rather than the row that happens to publish it (FR17).

Both are total: a cyclic chain stops rather than raising, matching the
finalize build walk's contract. The canonical cycle report is the
registry's own pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

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
    MERGE ORDER: each parent's own chain first (left to right), then the
    row itself.

    The order is the contract, not an implementation detail: a caller
    reads "the last layer that declared X" off it, so an order that
    diverged from the resolver's would attribute a value to a layer the
    resolver overrode. Pinned per kind by a test that folds these layers
    and gets the resolver's own merged result.

    A name with no row contributes nothing (an unresolved parent is the
    miss policy's to report), and a chain that revisits a name stops
    there rather than raising, because this runs inside the build walk.
    """
    layers: list[T] = []
    _collect(rows, name, (), layers)
    return tuple(layers)


def _collect[T: Inheriting](rows: Mapping[str, T], name: str, visiting: tuple[str, ...], out: list[T]) -> None:
    if name in visiting or name not in rows:
        return
    row = rows[name]
    for parent in row.inherits:
        _collect(rows, parent, (*visiting, name), out)
    out.append(row)


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
