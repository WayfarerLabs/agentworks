"""``RunContext``: the descriptive world as plain fields, the
power-granting world behind plain pass-through accessor methods.
"""

from __future__ import annotations

import dataclasses

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel
from agentworks.errors import ConfigError


class _Reader:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.reads: list[str] = []

    def get(self, name: str) -> str:
        self.reads.append(name)
        return self._values[name]


def test_targets_pass_through_unchanged() -> None:
    admin = object()
    agent = object()
    ctx = RunContext(admin_target=admin, agent_target=agent)  # type: ignore[arg-type]
    assert ctx.admin_target() is admin
    assert ctx.agent_target() is agent


def test_absent_targets_read_as_none() -> None:
    ctx = RunContext()
    assert ctx.admin_target() is None
    assert ctx.agent_target() is None


def test_secret_delegates_to_the_reader() -> None:
    reader = _Reader({"proxmox-token": "tok"})
    ctx = RunContext(secrets=reader)
    assert ctx.secret("proxmox-token") == "tok"
    assert reader.reads == ["proxmox-token"]


def test_secret_without_a_reader_is_a_typed_error() -> None:
    """Post-resolve code handed a pre-boundary (or inspection-only)
    context fails the same way for every capability."""
    with pytest.raises(ConfigError, match="resolved secrets"):
        RunContext().secret("proxmox-token")


def test_operation_scope_is_a_plain_field() -> None:
    scope = OperationScope(level=ScopeLevel.VM, vm="box")
    ctx = RunContext(operation_scope=scope)
    assert ctx.operation_scope is scope
    assert RunContext().operation_scope is None


def test_context_is_frozen() -> None:
    ctx = RunContext()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.config = None  # type: ignore[misc]
