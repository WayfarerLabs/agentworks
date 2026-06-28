"""Tests for compose_env, the helper that bridges scope env + identity + resolver."""

from __future__ import annotations

from agentworks.env import EnvEntry, ResourceContext, compose_env
from agentworks.secrets import SecretDecl, SecretResolver


class _FakeSource:
    kind = "env-var"

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def would_attempt(self, secret: SecretDecl) -> bool:  # noqa: ARG002
        return True

    def get(self, secret: SecretDecl) -> str | None:
        return self._values.get(secret.name)

    def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
        return {s.name: v for s in secrets if (v := self.get(s)) is not None}

    def describe_lookup(self, secret: SecretDecl) -> str | None:  # noqa: ARG002 - test stub
        return None


def _ctx(**overrides: object) -> ResourceContext:
    base: dict[str, object] = {
        "vm_name": "vm-1",
        "vm_host": "lima-local",
        "platform": "lima",
        "user": "agentworks",
    }
    base.update(overrides)
    return ResourceContext(**base)  # type: ignore[arg-type]


def _empty_resolver() -> SecretResolver:
    return SecretResolver([], {})


def test_compose_env_returns_only_per_context_identity_for_empty_env() -> None:
    """No env scopes set: result is just the per-context identity subset.
    VM-stable (VM/VM_HOST/PLATFORM) and per-user (USER) are NOT in the inline
    prelude (they live in Phase 4 profile fragments)."""
    out = compose_env(
        resolver=_empty_resolver(),
        ctx=_ctx(session_name="s1", session_kind="admin"),
        vm={},
    )
    assert out == {"AGENTWORKS_SESSION": "s1", "AGENTWORKS_SESSION_KIND": "admin"}


def test_compose_env_merges_scopes_with_identity_winning() -> None:
    """Identity vars override user env on collision: an operator who sets
    AGENTWORKS_SESSION_KIND in admin.env gets a warning at load time and
    no runtime effect (FRD R1)."""
    out = compose_env(
        resolver=_empty_resolver(),
        ctx=_ctx(session_name="s1", session_kind="admin"),
        vm={"EDITOR": EnvEntry(key="EDITOR", value="nvim")},
        admin={"AGENTWORKS_SESSION_KIND": EnvEntry(key="AGENTWORKS_SESSION_KIND", value="bogus")},
    )
    assert out["EDITOR"] == "nvim"
    assert out["AGENTWORKS_SESSION_KIND"] == "admin"  # identity wins


def test_compose_env_precedence_session_over_agent_over_vm() -> None:
    out = compose_env(
        resolver=_empty_resolver(),
        ctx=_ctx(),
        vm={"K": EnvEntry(key="K", value="from-vm")},
        agent={"K": EnvEntry(key="K", value="from-agent")},
        session={"K": EnvEntry(key="K", value="from-session")},
    )
    assert out["K"] == "from-session"


def test_compose_env_renders_secrets_via_resolver() -> None:
    decls = {"shared": SecretDecl(name="shared", description="x")}
    resolver = SecretResolver([_FakeSource({"shared": "resolved-value"})], decls)
    out = compose_env(
        resolver=resolver,
        ctx=_ctx(),
        vm={"API_KEY": EnvEntry(key="API_KEY", secret="shared")},
    )
    assert out["API_KEY"] == "resolved-value"


def test_compose_env_omits_vm_stable_vars() -> None:
    """The inline prelude does NOT include AGENTWORKS_VM / VM_HOST / PLATFORM.
    Those land in Phase 4 profile fragments on the VM."""
    out = compose_env(
        resolver=_empty_resolver(),
        ctx=_ctx(),
        vm={},
    )
    for excluded in ("AGENTWORKS_VM", "AGENTWORKS_VM_HOST", "AGENTWORKS_PLATFORM"):
        assert excluded not in out, f"{excluded} should not appear in inline prelude"
