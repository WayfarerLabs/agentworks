"""Tests for the SecretSource protocol defaults and dataclass shapes."""

from __future__ import annotations

import pytest

from agentworks.secrets import (
    SecretBackendConfig,
    SecretConfig,
    SecretDecl,
    SecretSource,
)


def test_secret_decl_defaults() -> None:
    d = SecretDecl(name="x", description="X")
    assert d.hint is None
    assert d.backend_mappings == {}


def test_secret_decl_carries_mappings() -> None:
    d = SecretDecl(
        name="x",
        description="X",
        backend_mappings={"env_var": "X_TOKEN", "onepassword": False, "vault": {"path": "x"}},
    )
    assert d.backend_mappings["env_var"] == "X_TOKEN"
    assert d.backend_mappings["onepassword"] is False
    assert d.backend_mappings["vault"] == {"path": "x"}


def test_secret_backend_config_carries_kind() -> None:
    assert SecretBackendConfig(kind="env_var").kind == "env_var"


def test_secret_config_default_empty() -> None:
    assert SecretConfig().backends == ()


def test_secret_config_preserves_order() -> None:
    cfg = SecretConfig(backends=("env_var", "onepassword", "prompt"))
    assert cfg.backends == ("env_var", "onepassword", "prompt")


def test_protocol_runtime_checkable_accepts_minimal_impl() -> None:
    """A minimal class implementing the protocol satisfies isinstance()."""

    class _MinimalSource:
        kind = "test"

        def would_attempt(self, secret: SecretDecl) -> bool:
            return True

        def get(self, secret: SecretDecl) -> str | None:
            return "ok"

        def batch_get(self, secrets: list[SecretDecl]) -> dict[str, str]:
            return {s.name: "ok" for s in secrets}

    assert isinstance(_MinimalSource(), SecretSource)


def test_protocol_rejects_missing_method() -> None:
    """Classes lacking the protocol methods fail isinstance()."""

    class _IncompleteSource:
        kind = "broken"
        # Missing: would_attempt, get, batch_get

    with pytest.raises(AssertionError):
        assert isinstance(_IncompleteSource(), SecretSource)
