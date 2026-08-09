"""``RealizationLog``: ordered record, reverse-order unwind, today's
rollback discipline (best-effort, never masks, ``UserAbort`` excepted).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentworks.errors import UserAbort
from agentworks.orchestration.unwind import RealizationLog


@dataclass
class _N:
    key: str
    log: list[str]
    fail_teardown: Exception | None = None
    realized: bool = field(default=False)

    def deps(self) -> tuple[_N, ...]:
        return ()

    def secret_refs(self) -> tuple[str, ...]:
        return ()

    def preflight(self, ctx: object) -> None: ...

    def runup(self, ctx: object) -> None: ...

    def mark_realized(self) -> None:
        self.realized = True
        self.log.append(f"realize:{self.key}")

    def teardown(self) -> None:
        if self.fail_teardown is not None:
            raise self.fail_teardown
        self.log.append(f"teardown:{self.key}")


def test_mark_realized_flips_and_records_in_order() -> None:
    events: list[str] = []
    a, b = _N("workspace/ws1", events), _N("agent/dev", events)
    log = RealizationLog()
    log.mark_realized(a)
    log.mark_realized(b)
    assert a.realized and b.realized
    assert [n.key for n in log.realized] == ["workspace/ws1", "agent/dev"]


def test_unwind_tears_down_in_reverse_realization_order() -> None:
    """The parity shape: reverse-of-creation, exactly today's rollback
    ordering discipline."""
    events: list[str] = []
    nodes = [_N("workspace/ws1", events), _N("agent/dev", events), _N("session/s1", events)]
    log = RealizationLog()
    for node in nodes:
        log.mark_realized(node)
    log.unwind()
    assert events[-3:] == [
        "teardown:session/s1",
        "teardown:agent/dev",
        "teardown:workspace/ws1",
    ]
    assert log.realized == ()


def test_failed_teardown_warns_and_never_masks(
    captured_output,
) -> None:
    """Best-effort: one node's failed teardown warns, the rest still
    tear down, and unwind returns so the caller's original error
    propagates unmasked."""
    events: list[str] = []
    a = _N("vm/box", events)
    b = _N("agent/dev", events, fail_teardown=RuntimeError("db locked"))
    c = _N("session/s1", events)
    log = RealizationLog()
    for node in (a, b, c):
        log.mark_realized(node)
    log.unwind()  # no raise
    assert events[-2:] == ["teardown:session/s1", "teardown:vm/box"]
    assert any("rollback: teardown of agent/dev failed: db locked" in w for w in captured_output.warnings)


def test_user_abort_during_teardown_is_reraised() -> None:
    """The exception to best-effort: an operator abort is never
    swallowed."""
    events: list[str] = []
    a = _N("vm/box", events)
    b = _N("agent/dev", events, fail_teardown=UserAbort("operator said stop"))
    log = RealizationLog()
    log.mark_realized(a)
    log.mark_realized(b)
    with pytest.raises(UserAbort):
        log.unwind()
    # The aborted node is still recorded; nothing beneath it was torn.
    assert [n.key for n in log.realized] == ["vm/box", "agent/dev"]
    assert "teardown:vm/box" not in events


def test_unwind_with_nothing_realized_is_a_no_op() -> None:
    RealizationLog().unwind()  # no raise, no output
