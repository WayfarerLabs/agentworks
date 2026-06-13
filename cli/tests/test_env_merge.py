"""Tests for the scope-merge precedence ladder."""

from __future__ import annotations

import pytest

from agentworks.env import EnvEntry, effective_env


def _e(key: str, value: str) -> EnvEntry:
    return EnvEntry(key=key, value=value)


def test_vm_only() -> None:
    out = effective_env(vm={"A": _e("A", "from-vm")})
    assert out == {"A": _e("A", "from-vm")}


def test_workspace_overrides_vm() -> None:
    out = effective_env(
        vm={"A": _e("A", "vm")},
        workspace={"A": _e("A", "ws")},
    )
    assert out["A"].value == "ws"


def test_agent_overrides_workspace_and_vm() -> None:
    out = effective_env(
        vm={"A": _e("A", "vm")},
        workspace={"A": _e("A", "ws")},
        agent={"A": _e("A", "agent")},
    )
    assert out["A"].value == "agent"


def test_admin_overrides_workspace_and_vm() -> None:
    out = effective_env(
        vm={"A": _e("A", "vm")},
        workspace={"A": _e("A", "ws")},
        admin={"A": _e("A", "admin")},
    )
    assert out["A"].value == "admin"


def test_session_overrides_agent() -> None:
    out = effective_env(
        vm={"A": _e("A", "vm")},
        agent={"A": _e("A", "agent")},
        session={"A": _e("A", "session")},
    )
    assert out["A"].value == "session"


def test_session_overrides_admin() -> None:
    out = effective_env(
        vm={"A": _e("A", "vm")},
        admin={"A": _e("A", "admin")},
        session={"A": _e("A", "session")},
    )
    assert out["A"].value == "session"


def test_admin_and_agent_both_set_raises() -> None:
    with pytest.raises(ValueError, match="admin / agent"):
        effective_env(
            vm={},
            admin={"A": _e("A", "x")},
            agent={"A": _e("A", "y")},
        )


def test_keys_from_disjoint_scopes_all_passthrough() -> None:
    out = effective_env(
        vm={"FROM_VM": _e("FROM_VM", "v")},
        workspace={"FROM_WS": _e("FROM_WS", "w")},
        agent={"FROM_AGENT": _e("FROM_AGENT", "a")},
        session={"FROM_SESSION": _e("FROM_SESSION", "s")},
    )
    assert set(out.keys()) == {"FROM_VM", "FROM_WS", "FROM_AGENT", "FROM_SESSION"}


def test_none_scopes_contribute_nothing() -> None:
    out = effective_env(
        vm={"A": _e("A", "vm")},
        workspace=None,
        admin=None,
        agent=None,
        session=None,
    )
    assert out == {"A": _e("A", "vm")}


def test_empty_scopes_contribute_nothing() -> None:
    out = effective_env(
        vm={"A": _e("A", "vm")},
        workspace={},
        session={},
    )
    assert out == {"A": _e("A", "vm")}
