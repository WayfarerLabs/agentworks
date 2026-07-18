"""Readiness driving helpers: the preflight sweep and the runup
policies.

Runup failure policy is the ORCHESTRATOR'S, not the node's: the node's
runup keeps its narrow contract (typed raise on definitive rejection,
warn-and-continue inside the node on network indeterminacy), and each
command decides what a raise means. Two policies exist today:

- FATAL: a plain uncaught raise; no helper needed (an explicit single
  add, or a platform check before anything exists, aborts whole).
- SKIP-AND-DEGRADE (:func:`runup_skip_and_degrade`): a definitive
  rejection skips that item's materials op and the command continues
  to a PARTIAL result; a retryable reinit recovers it later.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentworks.errors import TokenRejectedError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from agentworks.capabilities.base import RunContext

    from .node import Node, Readiness


def preflight_all(nodes: Iterable[Node], ctx: RunContext) -> None:
    """The preflight-all sweep: every participating node, against the
    one command-start context, before any prompt or mutation (the
    walk-away invariant's first half).

    ``nodes`` is normally a walk's output, so dependencies precede
    dependents; preflight is dependency-blind by contract, but the
    deterministic order keeps operator-facing check output stable. The
    first failure propagates: nothing has been touched yet, so there
    is nothing to unwind.
    """
    for node in nodes:
        node.preflight(ctx)


def runup_skip_and_degrade[T: Readiness](
    items: Iterable[T],
    ctx: RunContext,
    *,
    announce: Callable[[T], None] | None = None,
    on_reject: Callable[[T, TokenRejectedError], None],
) -> tuple[T, ...]:
    """The skip-and-degrade runup policy (the generalized form of the
    git-credential deferred runup): run each item's runup with ``ctx``;
    a definitive rejection (:class:`TokenRejectedError`, the runup
    contract's typed-rejection shape) SKIPS the item, so its materials
    op must not run, and hands it to ``on_reject`` for domain
    messaging; the command continues with the rest and degrades to a
    PARTIAL result (recording the warning that drives the degradation
    is ``on_reject``'s job, e.g. an init logger's warning count).
    Returns the items whose runup passed, in input order.

    ``items`` are ``Readiness``-typed, so both nodes and held
    capability instances fit. Indeterminacy never reaches here (the
    runup contract warns inside the item and returns); any other
    exception is not a rejection and propagates as the fatal policy
    would.
    """
    passed: list[T] = []
    for item in items:
        if announce is not None:
            announce(item)
        try:
            item.runup(ctx)
        except TokenRejectedError as exc:
            on_reject(item, exc)
            continue
        passed.append(item)
    return tuple(passed)
