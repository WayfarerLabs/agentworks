"""The ``inherits`` chain, as the framework sees it.

Four kinds inherit (``vm-template``, ``workspace-template``,
``agent-template``, ``session-template``). Their model trees own merge
policy, while this module owns the shared shape of the chain: an ordered
list of declarations, parents before the row itself, left to right.
:func:`resolution_layers` owns that one ordering for all four domains.
The generic layer fold records which declaration contributed each
surviving value, so no parallel declarer reconstruction remains.

The walk itself is :mod:`agentworks.traversal`'s. Resolution reports a
cycle to its caller; finalize views catch that typed error and degrade
until the registry's canonical cycle pass reports the complete problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NoReturn, Protocol

from agentworks.errors import inheritance_cycle_error
from agentworks.traversal import iter_post_order
from agentworks.value_provenance import LayerContribution as LayerContribution
from agentworks.value_provenance import LayerContributionKind as LayerContributionKind
from agentworks.value_provenance import ProvenancePath as ProvenancePath
from agentworks.value_provenance import longest_prefix_value

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping


class LayerSourceKind(StrEnum):
    """The honest origin of one declaration folded into an effective value."""

    DEFAULT = "default"
    TEMPLATE = "template"
    INSTANCE = "instance"


@dataclass(frozen=True)
class LayerSource:
    """Stable provenance identity for one ordered declaration layer."""

    kind: LayerSourceKind
    resource_kind: str
    name: str


@dataclass(frozen=True)
class DeclarationLayer[T]:
    """One typed declaration and the source that supplied it."""

    source: LayerSource
    declaration: T


@dataclass(frozen=True)
class LayeredResolution[T]:
    """A resolved value plus the contributing source for each preserved path.

    String path segments address fields and mapping keys. Integer segments
    address positions in the final effective list, never authored item
    values. A path may retain multiple sources when more than one layer
    contributed to a combined value.
    """

    value: T
    provenance: Mapping[ProvenancePath, tuple[LayerSource, ...]]


def run_layer_fold[A, D](
    seed: A,
    layers: Iterable[DeclarationLayer[D]],
    reducer: Callable[[A, D, LayerSource], tuple[A, Iterable[LayerContribution]]],
    *,
    default_paths: Iterable[ProvenancePath] = (),
    default_resource_kind: str,
    default_name: str = "built-in",
) -> LayeredResolution[A]:
    """Fold ordered declarations once and retain truthful value provenance.

    Reducers adapt domain declarations to the shared schema merge and return
    its value-path operations. The runner owns ordering and accumulation. A
    replacement path keeps only the current source. A contribution retains
    prior sources, seeding a newly materialized child path from its longest
    recorded prefix.
    """
    default_source = LayerSource(LayerSourceKind.DEFAULT, default_resource_kind, default_name)
    provenance: dict[ProvenancePath, tuple[LayerSource, ...]] = {path: (default_source,) for path in default_paths}
    result = seed
    for layer in layers:
        result, paths = reducer(result, layer.declaration, layer.source)
        for contribution in paths:
            if contribution.kind is LayerContributionKind.RESET_PREFIX:
                provenance = {
                    path: sources
                    for path, sources in provenance.items()
                    if path[: len(contribution.path)] != contribution.path
                }
                continue
            if contribution.kind is LayerContributionKind.REPLACEMENT:
                provenance[contribution.path] = (layer.source,)
                continue
            prior = longest_prefix_value(provenance, contribution.path) or ()
            if layer.source not in prior:
                provenance[contribution.path] = (*prior, layer.source)
    return LayeredResolution(result, provenance)


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


def resolution_layers[T: Inheriting](rows: Mapping[str, T], name: str, kind: str) -> tuple[T, ...]:
    """The declarations ``name``'s effective value is merged from, in
    MERGE ORDER, REFUSING a cyclic chain.

    Each parent's own chain appears first, left to right, followed by the
    row itself. A layer reachable by multiple routes appears once at its
    earliest position, as supplied by :func:`iter_post_order`. A missing
    name contributes no fabricated layer.

    A resolve has a caller that can report a cycle, so the walk raises the
    typed inheritance error here even though Registry owns the canonical
    finalize-time cycle pass.
    """
    walk = iter_post_order(
        name,
        lambda layer: _parents(rows, layer),
        on_cycle=lambda chain: _refuse(kind, chain),
    )
    return tuple(rows[layer] for layer in walk if layer in rows)


def _parents[T: Inheriting](rows: Mapping[str, T], name: str) -> tuple[str, ...]:
    """``name``'s declared parents, in declaration order. A name with no
    row has none, which is how an unresolved parent contributes nothing."""
    row = rows.get(name)
    return () if row is None else tuple(row.inherits)


def _refuse(kind: str, chain: tuple[str, ...]) -> NoReturn:
    raise inheritance_cycle_error(kind, chain)
