"""The preflight sweep: every node, one context, first failure wins."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import ConfigError
from agentworks.orchestration.readiness import preflight_all


@dataclass
class _N:
    key: str
    log: list[tuple[str, RunContext]]
    fail: bool = False
    _deps: tuple[_N, ...] = ()
    _secret_refs: tuple[str, ...] = field(default=())

    def deps(self) -> tuple[_N, ...]:
        return self._deps

    def secret_refs(self) -> tuple[str, ...]:
        return self._secret_refs

    def preflight(self, ctx: RunContext) -> None:
        self.log.append((self.key, ctx))
        if self.fail:
            raise ConfigError(f"{self.key}: not ready")

    def runup(self, ctx: RunContext) -> None: ...


def test_sweep_hits_every_node_in_order_with_one_context() -> None:
    log: list[tuple[str, RunContext]] = []
    nodes = [_N("vm-site/px", log), _N("git-credential/gh", log), _N("vm/box", log)]
    ctx = RunContext()
    preflight_all(nodes, ctx)
    assert [key for key, _ in log] == ["vm-site/px", "git-credential/gh", "vm/box"]
    assert all(seen is ctx for _, seen in log)


def test_sweep_propagates_the_first_failure() -> None:
    log: list[tuple[str, RunContext]] = []
    nodes = [
        _N("vm-site/px", log),
        _N("git-credential/gh", log, fail=True),
        _N("vm/box", log),
    ]
    with pytest.raises(ConfigError, match="git-credential/gh"):
        preflight_all(nodes, RunContext())
    # Nothing after the failure ran (the command aborts pre-mutation).
    assert [key for key, _ in log] == ["vm-site/px", "git-credential/gh"]


# -- the skip-and-degrade runup policy ---------------------------------------


class _RunupItem:
    def __init__(self, name: str, *, reject: bool = False, boom: bool = False) -> None:
        self.name = name
        self._reject = reject
        self._boom = boom

    def preflight(self, ctx: RunContext) -> None: ...

    def runup(self, ctx: RunContext) -> None:
        if self._boom:
            raise RuntimeError(f"{self.name}: not a rejection")
        if self._reject:
            from agentworks.errors import TokenRejectedError

            raise TokenRejectedError(f"{self.name}: token rejected")


def test_skip_and_degrade_keeps_passing_items_in_order() -> None:
    from agentworks.orchestration.readiness import runup_skip_and_degrade

    items = [_RunupItem("gh"), _RunupItem("ado")]
    passed = runup_skip_and_degrade(items, RunContext(), on_reject=lambda item, exc: None)
    assert [item.name for item in passed] == ["gh", "ado"]


def test_skip_and_degrade_skips_rejected_and_reports_them() -> None:
    """The partial-degradation shape runup_and_filter pins: a rejected
    item is dropped from the returned set and handed to the caller's
    messaging; the rest continue."""
    from agentworks.orchestration.readiness import runup_skip_and_degrade

    items = [_RunupItem("gh", reject=True), _RunupItem("ado")]
    rejected: list[tuple[str, str]] = []
    announced: list[str] = []
    passed = runup_skip_and_degrade(
        items,
        RunContext(),
        announce=lambda item: announced.append(item.name),
        on_reject=lambda item, exc: rejected.append((item.name, str(exc))),
    )
    assert [item.name for item in passed] == ["ado"]
    assert announced == ["gh", "ado"]
    assert rejected == [("gh", "gh: token rejected")]


def test_skip_and_degrade_lets_non_rejections_propagate() -> None:
    """Only the typed definitive rejection is policy; anything else is
    a bug or a fatal condition and propagates uncaught."""
    from agentworks.orchestration.readiness import runup_skip_and_degrade

    with pytest.raises(RuntimeError, match="not a rejection"):
        runup_skip_and_degrade(
            [_RunupItem("gh", boom=True)],
            RunContext(),
            on_reject=lambda item, exc: None,
        )
