"""Tests for the dataclass shapes in agentworks.secrets.base."""

from __future__ import annotations

from agentworks.secrets import (
    SecretConfig,
    SecretDecl,
)


def test_secret_decl_defaults() -> None:
    d = SecretDecl(name="x", description="X")
    assert d.hint is None
    assert d.backend_mappings == {}


def test_secret_decl_carries_mappings_keyed_by_backend_name() -> None:
    d = SecretDecl(
        name="x",
        description="X",
        backend_mappings={"env-var": "X_TOKEN", "op-work": False, "op-personal": {"vault": "p"}},
    )
    assert d.backend_mappings["env-var"] == "X_TOKEN"
    assert d.backend_mappings["op-work"] is False
    assert d.backend_mappings["op-personal"] == {"vault": "p"}


def test_secret_config_default_chain() -> None:
    """SecretConfig defaults to the standard env-var + prompt chain when no
    [secret_config].backends is provided. Operators who don't use secrets
    pay nothing; operators who do get sensible zero-config resolution."""
    from agentworks.secrets.base import DEFAULT_BACKEND_CHAIN

    assert SecretConfig().backends == DEFAULT_BACKEND_CHAIN
    assert DEFAULT_BACKEND_CHAIN == ("env-var", "prompt")


def test_secret_config_explicit_empty_disables_chain() -> None:
    """An explicit empty list opts out of resolution entirely (different
    from absence-of-config, which gets the default chain)."""
    assert SecretConfig(backends=()).backends == ()


def test_secret_config_preserves_order() -> None:
    cfg = SecretConfig(backends=("env-var", "op-work", "prompt"))
    assert cfg.backends == ("env-var", "op-work", "prompt")
