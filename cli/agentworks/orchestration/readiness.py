"""Readiness driving helpers: the preflight sweep.

The runup POLICY helpers (skip-and-degrade, generalizing today's
``git_credentials.runup_and_filter``) join this module with the first
command whose credential rejection degrades to partial (``vm create`` /
``vm reinit``); the fatal policy needs no helper (a plain uncaught
raise). Runup failure policy is the orchestrator's; the node's runup
keeps its narrow contract (typed raise on definitive rejection,
warn-and-continue inside the node on network indeterminacy).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agentworks.capabilities.base import RunContext

    from .node import Node


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
