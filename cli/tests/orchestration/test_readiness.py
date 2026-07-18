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
