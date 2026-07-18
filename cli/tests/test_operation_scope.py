"""``OperationScope``: the level-to-fields invariant is ENFORCED at
construction (a mis-leveled scope cannot exist), not documented in
prose.
"""

from __future__ import annotations

import dataclasses

import pytest

from agentworks.capabilities.base import OperationScope, ScopeLevel
from agentworks.errors import StateError


def test_system_scope_constructs_bare() -> None:
    scope = OperationScope(level=ScopeLevel.SYSTEM)
    assert scope.system_slug is None  # unset on a first-ever create
    assert scope.vm is None
    assert not scope.admin


def test_system_slug_is_allowed_at_every_constructible_level() -> None:
    OperationScope(level=ScopeLevel.SYSTEM, system_slug="lab")
    OperationScope(level=ScopeLevel.VM, system_slug="lab", vm="box")


def test_vm_scope_requires_its_vm() -> None:
    scope = OperationScope(level=ScopeLevel.VM, vm="box")
    assert scope.vm == "box"
    with pytest.raises(StateError, match=r"requires 'vm'"):
        OperationScope(level=ScopeLevel.VM)


@pytest.mark.parametrize("field", ["vm", "workspace", "agent", "session"])
def test_system_scope_forbids_deeper_names(field: str) -> None:
    with pytest.raises(StateError, match=f"forbids '{field}'"):
        OperationScope(level=ScopeLevel.SYSTEM, **{field: "x"})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["workspace", "agent", "session"])
def test_vm_scope_forbids_deeper_names(field: str) -> None:
    with pytest.raises(StateError, match=f"forbids '{field}'"):
        OperationScope(level=ScopeLevel.VM, vm="box", **{field: "x"})  # type: ignore[arg-type]


@pytest.mark.parametrize("level", [ScopeLevel.SYSTEM, ScopeLevel.VM])
def test_admin_is_session_vocabulary_only(level: ScopeLevel) -> None:
    kwargs = {"vm": "box"} if level is ScopeLevel.VM else {}
    with pytest.raises(StateError, match="admin"):
        OperationScope(level=level, admin=True, **kwargs)  # type: ignore[arg-type]


def test_error_names_every_violation_at_once() -> None:
    with pytest.raises(StateError, match=r"requires 'vm'.*forbids 'session'"):
        OperationScope(level=ScopeLevel.VM, session="s1")


@pytest.mark.parametrize("level", [ScopeLevel.WORKSPACE, ScopeLevel.AGENT])
def test_deeper_levels_are_loudly_not_constructible_yet(level: ScopeLevel) -> None:
    """The full five-level enum is a cheap contract defined up front,
    but a level's field rules land with the commands that operate at
    it; until then no scope with an unenforced invariant can exist.
    (SESSION landed with the session commands' migration.)"""
    with pytest.raises(StateError, match="cannot be constructed"):
        OperationScope(
            level=level, vm="box", workspace="ws1", agent="dev", session="s1"
        )


# -- the SESSION level (landed with the session commands) --------------------


def test_session_scope_agent_mode() -> None:
    scope = OperationScope(
        level=ScopeLevel.SESSION,
        vm="box",
        workspace="ws1",
        session="s1",
        agent="dev",
    )
    assert scope.agent == "dev" and not scope.admin


def test_session_scope_admin_mode() -> None:
    scope = OperationScope(
        level=ScopeLevel.SESSION,
        vm="box",
        workspace="ws1",
        session="s1",
        admin=True,
    )
    assert scope.agent is None and scope.admin


@pytest.mark.parametrize("field", ["vm", "workspace", "session"])
def test_session_scope_requires_its_chain(field: str) -> None:
    kwargs = {"vm": "box", "workspace": "ws1", "session": "s1", "agent": "dev"}
    del kwargs[field]
    with pytest.raises(StateError, match=f"requires '{field}'"):
        OperationScope(level=ScopeLevel.SESSION, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("agent,admin", [("dev", True), (None, False)])
def test_session_scope_requires_exactly_one_launch_identity(
    agent: str | None, admin: bool
) -> None:
    """A session runs as its agent OR as the admin: both and neither
    are equally mis-leveled."""
    with pytest.raises(StateError, match="exactly one of 'agent' or 'admin'"):
        OperationScope(
            level=ScopeLevel.SESSION,
            vm="box",
            workspace="ws1",
            session="s1",
            agent=agent,
            admin=admin,
        )


def test_scope_is_frozen() -> None:
    scope = OperationScope(level=ScopeLevel.VM, vm="box")
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.vm = "other"  # type: ignore[misc]
