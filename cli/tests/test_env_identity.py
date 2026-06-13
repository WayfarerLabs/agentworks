"""Tests for the AGENTWORKS_* identity producer."""

from __future__ import annotations

from agentworks.env import ResourceContext, agentworks_identity_env


def _ctx(**overrides: object) -> ResourceContext:
    base: dict[str, object] = {
        "vm_name": "vm-1",
        "vm_host": "lima-local",
        "platform": "lima",
        "user": "agentworks",
    }
    base.update(overrides)
    return ResourceContext(**base)  # type: ignore[arg-type]


def test_vm_only_emits_base_four_vars() -> None:
    out = agentworks_identity_env(_ctx())
    assert out == {
        "AGENTWORKS_VM": "vm-1",
        "AGENTWORKS_VM_HOST": "lima-local",
        "AGENTWORKS_PLATFORM": "lima",
        "AGENTWORKS_USER": "agentworks",
    }


def test_workspace_context_adds_workspace_vars() -> None:
    out = agentworks_identity_env(
        _ctx(workspace_name="ws-a", workspace_dir="/home/agentworks/ws-a"),
    )
    assert out["AGENTWORKS_WORKSPACE"] == "ws-a"
    assert out["AGENTWORKS_WORKSPACE_DIR"] == "/home/agentworks/ws-a"


def test_agent_context_adds_agent_var() -> None:
    out = agentworks_identity_env(_ctx(agent_name="claude"))
    assert out["AGENTWORKS_AGENT"] == "claude"


def test_session_context_adds_session_vars() -> None:
    out = agentworks_identity_env(
        _ctx(session_name="s1", session_kind="agent"),
    )
    assert out["AGENTWORKS_SESSION"] == "s1"
    assert out["AGENTWORKS_SESSION_KIND"] == "agent"


def test_session_kind_omitted_when_not_set() -> None:
    out = agentworks_identity_env(_ctx(session_name="s1"))
    assert "AGENTWORKS_SESSION" in out
    assert "AGENTWORKS_SESSION_KIND" not in out


def test_full_chain() -> None:
    out = agentworks_identity_env(
        _ctx(
            workspace_name="ws-a",
            workspace_dir="/home/claude/ws-a",
            agent_name="claude",
            session_name="s1",
            session_kind="agent",
        ),
    )
    assert out == {
        "AGENTWORKS_VM": "vm-1",
        "AGENTWORKS_VM_HOST": "lima-local",
        "AGENTWORKS_PLATFORM": "lima",
        "AGENTWORKS_USER": "agentworks",
        "AGENTWORKS_WORKSPACE": "ws-a",
        "AGENTWORKS_WORKSPACE_DIR": "/home/claude/ws-a",
        "AGENTWORKS_AGENT": "claude",
        "AGENTWORKS_SESSION": "s1",
        "AGENTWORKS_SESSION_KIND": "agent",
    }


def test_admin_session_kind() -> None:
    out = agentworks_identity_env(
        _ctx(session_name="admin-shell", session_kind="admin"),
    )
    assert out["AGENTWORKS_SESSION_KIND"] == "admin"
