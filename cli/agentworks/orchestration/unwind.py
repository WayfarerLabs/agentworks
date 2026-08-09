"""``RealizationLog``: the record unwind reads backwards.

The minimal shared plan-state in the model, and deliberately a list,
not a Plan class: what has been realized, in order. It is
COMMAND-LOCAL, instantiated by the orchestrator at the top of its
mutation phase; it lives on no context, no node, and no global (it is
the production form of the closure locals today's rollback blocks
capture).

Rollback split: nodes contribute their own ``teardown`` op (delete
what my realizing mutation made); the orchestrator owns WHEN, via this
log: reverse realization order, best-effort, and the original error is
never masked (the caller re-raises it after the unwind; a failed
teardown only warns). Non-rollbackable windows stay pinned per command
by its orchestrator, by simply not calling :meth:`unwind` there.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from agentworks import output
from agentworks.errors import UserAbort

if TYPE_CHECKING:
    from agentworks.orchestration.node import CreatableNode


class RealizationLog:
    """Ordered record of realized nodes; appended on
    :meth:`mark_realized`, read backwards on :meth:`unwind`."""

    def __init__(self) -> None:
        self._realized: list[CreatableNode] = []

    @property
    def realized(self) -> tuple[CreatableNode, ...]:
        """The realized nodes, in realization order."""
        return tuple(self._realized)

    def mark_realized(self, node: CreatableNode) -> None:
        """Flip ``node`` to realized and record it, called by the
        orchestrator immediately after the node's bespoke realizing
        mutation succeeds. Bookkeeping only: the mutation itself is
        orchestrator choreography, already done by the time this runs.
        """
        node.mark_realized()
        self._realized.append(node)

    def unwind(self) -> None:
        """Tear down every realized node, reverse realization order.

        Best-effort with today's rollback discipline: a failed
        teardown WARNS and the unwind continues. When called under an
        active primary failure, neither teardown control flow nor the
        warning sink may replace that primary. A standalone
        ``UserAbort`` remains the exception to best-effort and is
        re-raised. Every attempted node is dropped from the record
        whether or not its teardown succeeded under a primary; a
        standalone ``UserAbort``-interrupted node, and whatever was
        realized beneath it, stays recorded for a later call.
        """
        primary_failure = sys.exception()
        while self._realized:
            node = self._realized[-1]
            try:
                node.teardown()
            except BaseException as exc:
                if primary_failure is None and (isinstance(exc, UserAbort) or not isinstance(exc, Exception)):
                    raise
                try:
                    output.warn(f"rollback: teardown of {node.key} failed: {exc}")
                except BaseException:
                    if primary_failure is None:
                        raise
            self._realized.pop()
