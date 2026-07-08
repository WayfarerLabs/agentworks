"""Tests for compose_env: scope env + identity + pre-resolved values."""

from __future__ import annotations

import pytest

from agentworks.env import EnvEntry, ResourceContext, compose_env


def _ctx(**overrides: object) -> ResourceContext:
    base: dict[str, object] = {
        "vm_name": "vm-1",
        "vm_host": "lima-local",
        "platform": "lima",
        "user": "agentworks",
    }
    base.update(overrides)
    return ResourceContext(**base)  # type: ignore[arg-type]


def test_compose_env_returns_only_per_context_identity_for_empty_env() -> None:
    """No env scopes set: result is just the per-context identity subset.
    VM-stable (VM/VM_HOST/PLATFORM) and per-user (USER) are NOT in the inline
    prelude (they live in Phase 4 profile fragments)."""
    out = compose_env(
        values={},
        ctx=_ctx(session_name="s1", session_kind="admin"),
        vm={},
    )
    assert out == {"AGENTWORKS_SESSION": "s1", "AGENTWORKS_SESSION_KIND": "admin"}


def test_compose_env_merges_scopes_with_identity_winning() -> None:
    """Identity vars override user env on collision: an operator who sets
    AGENTWORKS_SESSION_KIND in admin.env gets a warning at load time and
    no runtime effect (FRD R1)."""
    out = compose_env(
        values={},
        ctx=_ctx(session_name="s1", session_kind="admin"),
        vm={"EDITOR": EnvEntry(key="EDITOR", value="nvim")},
        admin={"AGENTWORKS_SESSION_KIND": EnvEntry(key="AGENTWORKS_SESSION_KIND", value="bogus")},
    )
    assert out["EDITOR"] == "nvim"
    assert out["AGENTWORKS_SESSION_KIND"] == "admin"  # identity wins


def test_compose_env_precedence_session_over_agent_over_vm() -> None:
    out = compose_env(
        values={},
        ctx=_ctx(),
        vm={"K": EnvEntry(key="K", value="from-vm")},
        agent={"K": EnvEntry(key="K", value="from-agent")},
        session={"K": EnvEntry(key="K", value="from-session")},
    )
    assert out["K"] == "from-session"


def test_compose_env_renders_secrets_from_values() -> None:
    """Secret references render from the command's pre-resolved values
    dict -- the output of its one resolve_for_command call."""
    out = compose_env(
        values={"shared": "resolved-value"},
        ctx=_ctx(),
        vm={"API_KEY": EnvEntry(key="API_KEY", secret="shared")},
    )
    assert out["API_KEY"] == "resolved-value"


def test_compose_env_raises_loudly_on_uncovered_secret() -> None:
    """A secret reference absent from values means the eager-resolve
    target and this compose site drifted apart -- a bug in the calling
    command, surfaced loudly rather than resolved on the fly."""
    with pytest.raises(RuntimeError, match="drift"):
        compose_env(
            values={},
            ctx=_ctx(),
            vm={"API_KEY": EnvEntry(key="API_KEY", secret="uncovered")},
        )


def test_compose_env_omits_vm_stable_vars() -> None:
    """The inline prelude does NOT include AGENTWORKS_VM / VM_HOST / PLATFORM.
    Those land in Phase 4 profile fragments on the VM."""
    out = compose_env(
        values={},
        ctx=_ctx(),
        vm={},
    )
    for excluded in ("AGENTWORKS_VM", "AGENTWORKS_VM_HOST", "AGENTWORKS_PLATFORM"):
        assert excluded not in out, f"{excluded} should not appear in inline prelude"
