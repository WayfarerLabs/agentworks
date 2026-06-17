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


def test_vm_only_emits_base_three_vars() -> None:
    """Base case: only VM-stable identity. The on-VM Linux user is exposed
    via the standard ``$USER`` / ``$LOGNAME`` env vars, not a separate
    AGENTWORKS_-prefixed copy."""
    out = agentworks_identity_env(_ctx())
    assert out == {
        "AGENTWORKS_VM": "vm-1",
        "AGENTWORKS_VM_HOST": "lima-local",
        "AGENTWORKS_PLATFORM": "lima",
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


def test_vm_stable_subset_is_three_vars_only() -> None:
    """vm_stable_identity_env returns exactly the three vars that get written
    to /etc/profile.d/agentworks-identity.sh in Phase 4."""
    from agentworks.env import vm_stable_identity_env

    out = vm_stable_identity_env(_ctx())
    assert out == {
        "AGENTWORKS_VM": "vm-1",
        "AGENTWORKS_VM_HOST": "lima-local",
        "AGENTWORKS_PLATFORM": "lima",
    }


def test_per_context_subset_holds_workspace_and_session_only() -> None:
    """per_context_identity_env returns only the dynamic per-context vars
    (workspace and session). VM-stable vars are NOT included (they live
    in the system-wide profile fragment). AGENTWORKS_AGENT is NOT
    included either -- it's per-user-static now, written to the agent's
    ~/.agentworks-profile.sh and reached via login-shell sourcing."""
    from agentworks.env import per_context_identity_env, per_user_identity_env

    ctx = _ctx(
        workspace_name="ws-a",
        workspace_dir="/home/agentworks/ws-a",
        agent_name="claude",
        session_name="s1",
        session_kind="agent",
    )
    out = per_context_identity_env(ctx)
    assert "AGENTWORKS_VM" not in out
    assert "AGENTWORKS_PLATFORM" not in out
    assert "AGENTWORKS_AGENT" not in out
    assert out == {
        "AGENTWORKS_WORKSPACE": "ws-a",
        "AGENTWORKS_WORKSPACE_DIR": "/home/agentworks/ws-a",
        "AGENTWORKS_SESSION": "s1",
        "AGENTWORKS_SESSION_KIND": "agent",
    }
    # AGENTWORKS_AGENT lives in the per-user subset instead.
    assert per_user_identity_env(ctx) == {"AGENTWORKS_AGENT": "claude"}


def test_per_context_subset_minimal_when_no_scope() -> None:
    """When only the base VM/user fields are set (no workspace / agent /
    session), per_context_identity_env returns an empty dict."""
    from agentworks.env import per_context_identity_env

    assert per_context_identity_env(_ctx()) == {}
