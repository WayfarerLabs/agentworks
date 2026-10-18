"""``RunContext``: the descriptive world as plain fields, the
power-granting world behind plain pass-through accessor methods.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.capabilities.base import OperationScope, RunContext, ScopeLevel, TargetAbsence
from agentworks.errors import ConfigError

if TYPE_CHECKING:
    from agentworks.execution.target import ExecutionTarget
    from agentworks.transports import Transport


class _UntouchableTarget:
    """Only identity may be observed at the passive context boundary."""

    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"Context inspected a target: {name}")


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
    assert ctx.admin_execution_target() is None
    assert ctx.agent_execution_target() is None
    assert ctx.platform_host_execution_target() is None
    assert ctx.admin_execution_target_absence() is TargetAbsence.UNAVAILABLE
    assert ctx.agent_execution_target_absence() is TargetAbsence.UNAVAILABLE
    assert ctx.platform_host_execution_target_absence() is TargetAbsence.UNAVAILABLE


def test_absent_targets_read_as_none() -> None:
    ctx = RunContext()
    assert ctx.admin_target() is None
    assert ctx.agent_target() is None
    assert ctx.admin_execution_target() is None
    assert ctx.agent_execution_target() is None
    assert ctx.platform_host_execution_target() is None


@pytest.mark.parametrize("bind_admin", [False, True])
@pytest.mark.parametrize("bind_agent", [False, True])
@pytest.mark.parametrize("admin_absence", list(TargetAbsence))
@pytest.mark.parametrize("agent_absence", list(TargetAbsence))
def test_execution_bindings_are_independent_and_passive(
    bind_admin: bool, bind_agent: bool, admin_absence: TargetAbsence, agent_absence: TargetAbsence
) -> None:
    # Typed sentinels avoid constructing carriers: context only stores identities.
    admin = cast("ExecutionTarget", _UntouchableTarget()) if bind_admin else None
    agent = cast("ExecutionTarget", _UntouchableTarget()) if bind_agent else None
    legacy_admin = cast("Transport", _UntouchableTarget())
    legacy_agent = cast("Transport", _UntouchableTarget())
    reader = _Reader({"secret": "value"})
    ctx = RunContext(
        admin_target=legacy_admin,
        agent_target=legacy_agent,
        admin_execution_target=admin,
        agent_execution_target=agent,
        admin_execution_target_absence=admin_absence,
        agent_execution_target_absence=agent_absence,
        secrets=reader,
    )

    for _ in range(2):
        assert ctx.admin_execution_target() is admin
        assert ctx.agent_execution_target() is agent
        assert ctx.admin_target() is legacy_admin
        assert ctx.agent_target() is legacy_agent
        assert ctx.admin_execution_target_absence() is (None if bind_admin else admin_absence)
        assert ctx.agent_execution_target_absence() is (None if bind_agent else agent_absence)
    assert reader.reads == []


def test_execution_bindings_do_not_supply_legacy_transports() -> None:
    ctx = RunContext(
        admin_execution_target=cast("ExecutionTarget", _UntouchableTarget()),
        agent_execution_target=cast("ExecutionTarget", _UntouchableTarget()),
    )
    assert ctx.admin_target() is None
    assert ctx.agent_target() is None


def test_platform_host_binding_is_independent_and_passive() -> None:
    host = cast("ExecutionTarget", _UntouchableTarget())
    ctx = RunContext(platform_host_execution_target=host)
    for _ in range(2):
        assert ctx.platform_host_execution_target() is host
        assert ctx.platform_host_execution_target_absence() is None
    assert ctx.admin_execution_target() is None
    assert ctx.agent_execution_target() is None
    assert ctx.admin_target() is None
    assert ctx.agent_target() is None


def test_platform_host_and_guest_bindings_remain_independent() -> None:
    host = cast("ExecutionTarget", _UntouchableTarget())
    admin = cast("ExecutionTarget", _UntouchableTarget())
    agent = cast("ExecutionTarget", _UntouchableTarget())
    legacy_admin = cast("Transport", _UntouchableTarget())
    legacy_agent = cast("Transport", _UntouchableTarget())
    reader = _Reader({"secret": "value"})
    ctx = RunContext(
        admin_target=legacy_admin,
        agent_target=legacy_agent,
        admin_execution_target=admin,
        agent_execution_target=agent,
        platform_host_execution_target=host,
        secrets=reader,
    )

    for _ in range(2):
        assert ctx.platform_host_execution_target() is host
    assert ctx.admin_execution_target() is admin
    assert ctx.agent_execution_target() is agent
    assert ctx.admin_target() is legacy_admin
    assert ctx.agent_target() is legacy_agent
    assert ctx.platform_host_execution_target_absence() is None
    assert ctx.admin_execution_target_absence() is None
    assert ctx.agent_execution_target_absence() is None
    assert reader.reads == []


@pytest.mark.parametrize("absence", list(TargetAbsence))
def test_absent_platform_host_binding_preserves_reason(absence: TargetAbsence) -> None:
    guest = cast("ExecutionTarget", _UntouchableTarget())
    ctx = RunContext(
        agent_execution_target=guest,
        platform_host_execution_target_absence=absence,
    )
    assert ctx.platform_host_execution_target() is None
    assert ctx.platform_host_execution_target_absence() is absence
    assert ctx.agent_execution_target() is guest


@pytest.mark.parametrize(
    "field",
    [
        "_admin_execution_target",
        "_agent_execution_target",
        "_admin_execution_target_absence",
        "_agent_execution_target_absence",
        "_platform_host_execution_target",
        "_platform_host_execution_target_absence",
    ],
)
def test_execution_bindings_are_frozen(field: str) -> None:
    admin = cast("ExecutionTarget", _UntouchableTarget())
    agent = cast("ExecutionTarget", _UntouchableTarget())
    host = cast("ExecutionTarget", _UntouchableTarget())
    ctx = RunContext(
        admin_execution_target=admin,
        agent_execution_target=agent,
        platform_host_execution_target=host,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(ctx, field, None)
    assert ctx.admin_execution_target() is admin
    assert ctx.agent_execution_target() is agent
    assert ctx.platform_host_execution_target() is host


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
    assert ctx.admin_execution_target() is None
    assert ctx.agent_execution_target() is None
    assert RunContext().operation_scope is None


def test_context_is_frozen() -> None:
    ctx = RunContext()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.config = None  # type: ignore[misc]
